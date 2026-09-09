"""Parameterized synthetic monthly-universe decisions owned by the host."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, time
from decimal import Context, Decimal, localcontext
from statistics import median
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from stock_profiler.modules.candidate_selection.universe_evidence import UniverseDataManifest
from stock_profiler.modules.qualification.contracts import (
    CapabilityVersion,
    GovernanceOutcome,
    QualificationRecord,
    QualificationScope,
)
from stock_profiler.modules.qualification.service import (
    current_qualification,
    qualification_is_current,
)


class UniverseContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniversePolicy(UniverseContract):
    version_id: str = Field(min_length=1)
    minimum_listing_months: int = Field(gt=0)
    turnover_sessions: int = Field(gt=0)
    minimum_median_turnover: Decimal = Field(gt=0, allow_inf_nan=False)
    maximum_participation: Decimal = Field(gt=0, le=1, allow_inf_nan=False)


class UniverseSecurity(UniverseContract):
    security_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    board: str
    asset_type: str
    listed_on: date | None
    risk_warning: bool | None
    delisting: bool | None
    suspended: bool | None
    daily_turnover: tuple[Decimal, ...]
    turnover_dates: tuple[date, ...]
    unit_price: Decimal = Field(gt=0, allow_inf_nan=False)
    minimum_quantity: int = Field(gt=0, strict=True)
    quantity_increment: int = Field(gt=0, strict=True)
    acquisition_fixed_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    acquisition_cost_ratio: Decimal = Field(ge=0, allow_inf_nan=False)
    rules_valid_from: AwareDatetime
    rules_valid_until: AwareDatetime
    planned_amount: Decimal = Field(ge=0, allow_inf_nan=False)
    minimum_unit_cost: Decimal = Field(gt=0, allow_inf_nan=False)
    available_budget: Decimal = Field(ge=0, allow_inf_nan=False)


class UniverseCalendarDay(UniverseContract):
    market_date: date
    close_at: AwareDatetime | None


class UniverseCalendar(UniverseContract):
    version_id: str = Field(min_length=1)
    days: tuple[UniverseCalendarDay, ...]

    def cutoff_valid(self, command_cutoff: AwareDatetime) -> bool:
        cutoff = command_cutoff.astimezone(ZoneInfo("Asia/Shanghai"))
        dates = [day.market_date for day in self.days]
        month_dates = [day for day in dates if (day.year, day.month) == (cutoff.year, cutoff.month)]
        expected = [
            date(cutoff.year, cutoff.month, day)
            for day in range(1, monthrange(cutoff.year, cutoff.month)[1] + 1)
        ]
        sessions = [
            day
            for day in self.days
            if day.close_at is not None
            and (day.market_date.year, day.market_date.month) == (cutoff.year, cutoff.month)
        ]
        return (
            dates == sorted(set(dates))
            and month_dates == expected
            and bool(sessions)
            and sessions[-1].market_date == cutoff.date()
            and cutoff.time() == time(23, 59, 59)
            and all(
                day.close_at is None
                or day.close_at.astimezone(ZoneInfo("Asia/Shanghai")).date() == day.market_date
                for day in self.days
            )
        )


class UniversePermission(UniverseContract):
    board: str
    state: Literal["GRANTED", "DENIED", "UNKNOWN"]
    risk_disclosure: bool


class UniverseEntitlements(UniverseContract):
    account_id: str
    snapshot_at: AwareDatetime
    permissions: tuple[UniversePermission, ...]


class BoardQualification(UniverseContract):
    scope: QualificationScope
    version: CapabilityVersion


class UniverseCommand(UniverseContract):
    contract_version: Literal["1.0.0"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    cutoff_at: AwareDatetime
    purpose: Literal["SYNTHETIC", "HISTORICAL_RECONSTRUCTED", "REAL_CANDIDATE"]
    account_ids: tuple[str, ...] = Field(min_length=1)
    policy: UniversePolicy
    securities: tuple[UniverseSecurity, ...]
    security_inventory: tuple[str, ...]
    manifest: UniverseDataManifest | None = None
    calendar: UniverseCalendar | None = None
    entitlements: tuple[UniverseEntitlements, ...] = ()
    market_state: str = Field(min_length=1)
    board_qualifications: tuple[BoardQualification, ...] = ()

    def data_payloads(self) -> dict[str, object]:
        return {
            "INVENTORY": list(self.security_inventory),
            "CALENDAR": self.calendar.model_dump(mode="json") if self.calendar else None,
            "ENTITLEMENTS": [item.model_dump(mode="json") for item in self.entitlements],
            **{
                family: [
                    security.model_dump(mode="json", include=fields) for security in self.securities
                ]
                for family, fields in {
                    "SECURITIES": {
                        "security_id",
                        "board",
                        "asset_type",
                        "listed_on",
                        "risk_warning",
                        "delisting",
                        "suspended",
                    },
                    "MARKET": {"security_id", "daily_turnover", "turnover_dates", "unit_price"},
                    "RULES": {
                        "security_id",
                        "board",
                        "minimum_quantity",
                        "quantity_increment",
                        "rules_valid_from",
                        "rules_valid_until",
                    },
                    "AFFORDABILITY": {
                        "security_id",
                        "planned_amount",
                        "minimum_unit_cost",
                        "available_budget",
                        "account_id",
                        "acquisition_fixed_cost",
                        "acquisition_cost_ratio",
                    },
                }.items()
            },
        }


class UniverseExclusion(UniverseContract):
    security_id: str
    reasons: tuple[str, ...]


class UniverseOutcome(UniverseContract):
    disposition: Literal["FROZEN", "DATA_FAILED", "BLOCKED"]
    cutoff_at: AwareDatetime
    policy: UniversePolicy
    manifest: UniverseDataManifest | None = None
    members: tuple[str, ...]
    exclusions: tuple[UniverseExclusion, ...]
    reasons: tuple[str, ...]
    qualification_scope: Literal["D0_SYNTHETIC_CONTRACT_ONLY"]
    actionable: Literal[False] = False
    board_qualifications: tuple[QualificationRecord, ...] = ()


def freeze_universe(
    command: UniverseCommand,
    *,
    history: tuple[GovernanceOutcome, ...] = (),
    business_prerequisite_met: bool = True,
) -> UniverseOutcome:
    if not business_prerequisite_met:
        return UniverseOutcome(
            disposition="BLOCKED",
            cutoff_at=command.cutoff_at,
            policy=command.policy,
            manifest=command.manifest,
            members=(),
            exclusions=(),
            reasons=("BUSINESS_PREREQUISITE_NOT_MET",),
            qualification_scope="D0_SYNTHETIC_CONTRACT_ONLY",
        )
    failure_reasons: list[str] = []
    security_ids = [security.security_id for security in command.securities]
    if (
        len(set(security_ids)) != len(security_ids)
        or len(set(command.security_inventory)) != len(command.security_inventory)
        or set(security_ids) != set(command.security_inventory)
    ):
        failure_reasons.append("SECURITY_INVENTORY_INCOMPLETE")
    if any(
        security.listed_on is None
        or security.risk_warning is None
        or security.delisting is None
        or security.suspended is None
        for security in command.securities
    ):
        failure_reasons.append("SECURITY_FACT_UNKNOWN")
    if command.calendar is None or not command.calendar.cutoff_valid(command.cutoff_at):
        failure_reasons.append("MONTHLY_CUTOFF_INVALID")
    close = (
        next(
            (
                day.close_at
                for day in command.calendar.days
                if day.market_date == command.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            ),
            None,
        )
        if command.calendar is not None
        else None
    )
    if command.entitlements:
        accounts = [item.account_id for item in command.entitlements]
        if len(set(accounts)) != len(accounts) or set(accounts) != set(command.account_ids):
            failure_reasons.append("ENTITLEMENTS_SCOPE_INCOMPLETE")
        for entitlement in command.entitlements:
            if command.calendar is not None and (
                close is None or not close < entitlement.snapshot_at <= command.cutoff_at
            ):
                failure_reasons.append("ENTITLEMENTS_STALE")
            boards = [permission.board for permission in entitlement.permissions]
            if len(set(boards)) != len(boards):
                failure_reasons.append("ENTITLEMENTS_SCOPE_INCOMPLETE")
            if any(permission.state == "UNKNOWN" for permission in entitlement.permissions):
                failure_reasons.append("ACCOUNT_PERMISSION_UNKNOWN")
    if command.manifest is None:
        failure_reasons.append("DATA_MANIFEST_REQUIRED")
    else:
        payloads = command.data_payloads()
        families = [entry.field_family for entry in command.manifest.entries]
        if len(families) != len(set(families)) or not set(payloads).issubset(families):
            failure_reasons.append("MANIFEST_COVERAGE_FAILED")
        for entry in command.manifest.entries:
            if entry.requirement == "EXPLORATORY":
                if entry.field_family in payloads:
                    failure_reasons.append("REQUIRED_DATA_DOWNGRADED")
                continue
            if entry.field_family in payloads:
                authority = (
                    "BROKER"
                    if entry.field_family in {"ENTITLEMENTS", "AFFORDABILITY"}
                    else "EXCHANGE"
                )
                failure_reasons.extend(
                    entry.failures(
                        cutoff=command.cutoff_at,
                        expected=payloads[entry.field_family],
                        authority=authority,
                        manifest_version=command.manifest.version_id,
                        purpose=command.purpose,
                    )
                )
            else:
                failure_reasons.append("UNSUPPORTED_REQUIRED_FAMILY")
            if entry.evidence is None:
                continue
            proofs = (entry.evidence,) + (
                (entry.substitution.authority_evidence,) if entry.substitution else ()
            )
            for proof in proofs:
                if entry.field_family == "ENTITLEMENTS" and any(
                    proof.acquired_at is None or entitlement.snapshot_at > proof.acquired_at
                    for entitlement in command.entitlements
                ):
                    failure_reasons.append("ENTITLEMENTS_EVIDENCE_CLOCK")
                if entry.field_family in {"MARKET", "SECURITIES", "AFFORDABILITY", "INVENTORY"}:
                    local = proof.fact_effective_at.astimezone(ZoneInfo("Asia/Shanghai"))
                    if (
                        local.date()
                        != command.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
                    ):
                        failure_reasons.append("CURRENT_FACT_STALE")
                if entry.field_family == "MARKET" and (
                    close is None
                    or any(
                        instant is None or instant < close
                        for instant in (
                            proof.fact_effective_at,
                            proof.source_published_at,
                            proof.source_observed_at,
                            proof.acquired_at,
                            proof.validated_at,
                        )
                    )
                ):
                    failure_reasons.append("CLOSING_MARKET_EVIDENCE_REQUIRED")
    if any(
        len(security.daily_turnover) != command.policy.turnover_sessions
        or any(not amount.is_finite() or amount < 0 for amount in security.daily_turnover)
        for security in command.securities
    ):
        failure_reasons.append("MARKET_WINDOW_INCOMPLETE")
    if command.calendar is not None:
        sessions = tuple(
            day.market_date
            for day in command.calendar.days
            if day.close_at is not None and day.close_at <= command.cutoff_at
        )[-command.policy.turnover_sessions :]
        if len(sessions) != command.policy.turnover_sessions or any(
            security.turnover_dates != sessions for security in command.securities
        ):
            failure_reasons.append("MARKET_WINDOW_INCOMPLETE")
    with localcontext(Context(prec=38)):
        for security in command.securities:
            if security.account_id not in command.account_ids:
                failure_reasons.append("AFFORDABILITY_ACCOUNT_MISMATCH")
            if not security.rules_valid_from <= command.cutoff_at <= security.rules_valid_until:
                failure_reasons.append("TRADING_RULES_EXPIRED")
            principal = security.unit_price * security.minimum_quantity
            total = (
                principal
                + security.acquisition_fixed_cost
                + principal * security.acquisition_cost_ratio
            )
            if total != security.minimum_unit_cost:
                failure_reasons.append("ACQUISITION_COST_MISMATCH")
    if failure_reasons:
        return UniverseOutcome(
            disposition="DATA_FAILED",
            cutoff_at=command.cutoff_at,
            policy=command.policy,
            manifest=command.manifest,
            members=(),
            exclusions=(),
            reasons=tuple(failure_reasons),
            qualification_scope="D0_SYNTHETIC_CONTRACT_ONLY",
        )
    if command.purpose == "REAL_CANDIDATE" and not command.entitlements:
        return UniverseOutcome(
            disposition="BLOCKED",
            cutoff_at=command.cutoff_at,
            policy=command.policy,
            manifest=command.manifest,
            members=(),
            exclusions=(),
            reasons=("REAL_ENTITLEMENTS_REQUIRED", "D0_CANNOT_AUTHORIZE_REAL_CANDIDATES"),
            qualification_scope="D0_SYNTHETIC_CONTRACT_ONLY",
        )
    members: list[str] = []
    exclusions: list[UniverseExclusion] = []
    qualifications: list[QualificationRecord] = []
    for binding in command.board_qualifications:
        scope = binding.scope
        if (
            scope.capability != "INVESTABLE_UNIVERSE"
            or scope.purpose != command.purpose
            or scope.target != "MEMBERSHIP"
            or scope.market_state != command.market_state
            or scope.account_type != "SIMULATED_CASH"
            or set(scope.account_ids) != set(command.account_ids)
            or command.manifest is None
            or scope.source != command.manifest.version_id
            or binding.version.policy_version != command.policy.version_id
        ):
            continue
        try:
            visible_history = tuple(
                outcome
                for outcome in history
                if outcome.qualification is not None
                and outcome.qualification.recorded_at <= command.cutoff_at
            )
            record = current_qualification(visible_history, scope, binding.version)
        except ValueError:
            continue
        if (
            record is not None
            and record.status in {"VALID", "AT_RISK"}
            and record.recorded_at <= command.cutoff_at
            and record.evidence_available_by(command.cutoff_at)
            and qualification_is_current(record, command.cutoff_at)
        ):
            qualifications.append(record)
    enabled_boards = {"SH_MAIN", "SZ_MAIN"} | {
        record.scope.board
        for record in qualifications
        if record.scope.board in {"CHINEXT", "STAR", "BSE"} and command.entitlements
    }
    selected_on = command.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    with localcontext(Context(prec=38)):
        for security in sorted(command.securities, key=lambda item: item.security_id):
            assert security.listed_on is not None
            reasons: list[str] = []
            if security.asset_type != "RMB_ORDINARY_SHARE":
                reasons.append("UNSUPPORTED_ASSET")
            if security.board not in enabled_boards:
                reasons.append("BOARD_NOT_ENABLED")
            if command.entitlements and not any(
                permission.board == security.board
                and permission.state == "GRANTED"
                and permission.risk_disclosure
                for entitlement in command.entitlements
                if entitlement.account_id == security.account_id
                for permission in entitlement.permissions
            ):
                reasons.append("ACCOUNT_PERMISSION_DENIED")
            if security.risk_warning:
                reasons.append("RISK_WARNING")
            if security.delisting:
                reasons.append("DELISTING")
            if security.suspended:
                reasons.append("SUSPENDED")
            elapsed_months = (
                (selected_on.year - security.listed_on.year) * 12
                + selected_on.month
                - security.listed_on.month
            )
            anniversary_day = min(
                security.listed_on.day, monthrange(selected_on.year, selected_on.month)[1]
            )
            if elapsed_months < command.policy.minimum_listing_months or (
                elapsed_months == command.policy.minimum_listing_months
                and selected_on.day < anniversary_day
            ):
                reasons.append("LISTING_IMMATURE")
            turnover = Decimal(median(security.daily_turnover))
            if turnover < command.policy.minimum_median_turnover:
                reasons.append("INSUFFICIENT_LIQUIDITY")
            if security.planned_amount > turnover * command.policy.maximum_participation:
                reasons.append("PARTICIPATION_LIMIT")
            if security.minimum_unit_cost > security.available_budget:
                reasons.append("UNAFFORDABLE_UNIT")
            if reasons:
                exclusions.append(
                    UniverseExclusion(security_id=security.security_id, reasons=tuple(reasons))
                )
            else:
                members.append(security.security_id)
    return UniverseOutcome(
        disposition="FROZEN",
        cutoff_at=command.cutoff_at,
        policy=command.policy,
        manifest=command.manifest,
        members=tuple(members),
        exclusions=tuple(exclusions),
        reasons=(),
        qualification_scope="D0_SYNTHETIC_CONTRACT_ONLY",
        board_qualifications=tuple(qualifications),
    )
