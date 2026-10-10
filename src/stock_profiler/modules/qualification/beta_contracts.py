"""Frozen synthetic envelope inputs; these never confer personal authority."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from stock_profiler.modules.qualification.contracts import (
    CapabilityVersion,
    GovernanceContract,
    QualificationScope,
)

BetaRole = Literal["B0", "B1", "T0", "T1", "T2", "T3", "T4", "G1", "G2", "G3", "G4", "G5"]
BETA_REQUIRED_ROLES: tuple[BetaRole, ...] = (
    "B0",
    "B1",
    "T0",
    "T1",
    "T2",
    "T3",
    "T4",
    "G1",
    "G2",
    "G3",
    "G4",
    "G5",
)


class SyntheticBetaInput(GovernanceContract):
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int

    @field_validator("synthetic", mode="before")
    @classmethod
    def explicit_synthetic(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("beta input must explicitly declare synthetic: true")
        return True


class BetaPolicy(GovernanceContract):
    version_id: str = Field(min_length=1)
    initial_ratio: Decimal = Field(gt=0, lt=1)
    expanded_ratio: Decimal = Field(gt=0, lt=1)
    planned_month_count: int = Field(ge=3)
    clean_month_count: int = Field(ge=3)
    position_day_count: int = Field(ge=1)
    minimum_completion: Decimal = Field(gt=0, le=1)
    statistical_age_domain: str = Field(min_length=1)
    statistical_probability_grid: str = Field(min_length=1)
    statistical_source: str = Field(min_length=1)
    statistical_target: str = Field(min_length=1)
    statistical_purpose: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_ratios(self) -> "BetaPolicy":
        if self.expanded_ratio <= self.initial_ratio:
            raise ValueError("expanded envelope must exceed the initial envelope")
        return self


class BetaQualificationBinding(GovernanceContract):
    role: BetaRole
    decision_id: str = Field(min_length=1)
    version: CapabilityVersion


class BetaMonth(GovernanceContract):
    plan_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    closed_at: AwareDatetime
    batch_required: int = Field(ge=0)
    batch_completed: int = Field(ge=0)
    timely_required: int = Field(ge=0)
    timely_completed: int = Field(ge=0)
    data_completed: bool
    pipeline_completed: bool
    plan_required: int = Field(ge=0)
    plan_completed: int = Field(ge=0)
    notification_required: int = Field(ge=0)
    notification_completed: int = Field(ge=0)
    report_required: int = Field(ge=0)
    report_completed: int = Field(ge=0)
    confirmation_required: int = Field(ge=0)
    confirmation_completed: int = Field(ge=0)
    reconciliation_required: int = Field(ge=0)
    reconciliation_completed: int = Field(ge=0)
    safety_failures: tuple[str, ...]

    @model_validator(mode="after")
    def valid_denominators(self) -> "BetaMonth":
        for name in (
            "batch",
            "timely",
            "plan",
            "notification",
            "report",
            "confirmation",
            "reconciliation",
        ):
            if getattr(self, f"{name}_completed") > getattr(self, f"{name}_required"):
                raise ValueError("completion cannot exceed original obligations")
        return self


class BetaDay(GovernanceContract):
    closed_at: AwareDatetime
    data_completed: bool
    pipeline_completed: bool
    system_position_ids: tuple[str, ...]
    p0p1_missing: int = Field(ge=0)
    safety_failures: tuple[str, ...]


class BetaTerminalPlan(GovernanceContract):
    plan_id: str = Field(min_length=1)
    confirmation_id: str = Field(min_length=1)
    terminal_outcome: Literal[
        "NO_TRADE", "ALL_DECLINED", "DEFERRED_EXPIRED", "PARTIALLY_FILLED", "FULLY_FILLED"
    ]
    broker_order_ids: tuple[str, ...]
    fill_ids: tuple[str, ...]
    funds_reconciliation_id: str | None
    position_reconciliation_id: str | None
    accepted_intents: int = Field(ge=0)
    remaining_reservation: Decimal = Field(ge=0)
    unknown_orders: tuple[str, ...]
    execution_deviations_reconciled: bool
    authoritative: bool
    closed_at: AwareDatetime


class BetaObservations(SyntheticBetaInput):
    scope: QualificationScope
    activation_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    qualification_bindings: tuple[BetaQualificationBinding, ...]
    simulated_origin: Literal["REAL_TIME", "REPLAY", "HISTORICAL", "POC"]
    activated_at: AwareDatetime
    registered_at: AwareDatetime
    available_at: AwareDatetime
    plan_months: tuple[str, ...]
    months: tuple[BetaMonth, ...]
    market_calendar_version: str = Field(min_length=1)
    plan_days: tuple[AwareDatetime, ...]
    days: tuple[BetaDay, ...]
    terminal_plans: tuple[BetaTerminalPlan, ...]
    expansion_confirmed: bool
    effective_at: AwareDatetime


class BetaMetric(GovernanceContract):
    required: int
    completed: int
    rate: Decimal | None
    status: Literal["PASSED", "FAILED", "NOT_APPLICABLE"]


class BetaOperations(GovernanceContract):
    plan_months: tuple[str, ...]
    missing_months: tuple[str, ...]
    window_complete: bool
    metrics: dict[str, BetaMetric]
    consecutive_core_failure: bool
    safety_failures: tuple[str, ...]
    passed: bool


class BetaEnvelopeCommand(SyntheticBetaInput):
    activation_id: str = Field(min_length=1)
    requested_envelope: Literal["INITIAL", "EXPANDED"]
    scope: QualificationScope
    policy: BetaPolicy
    qualification_bindings: tuple[BetaQualificationBinding, ...] = ()
    observations: BetaObservations | None = Field(default=None, exclude_if=lambda v: v is None)


class BetaEnvelopeOutcome(GovernanceContract):
    activation_id: str
    permission_envelope: Literal["INITIAL", "EXPANDED"]
    ratio: Decimal
    net_liquidation_equity: Decimal
    normal_feasible_capacity: Decimal
    committed_exposure: Decimal
    capacity: Decimal
    qualification_decision_ids: tuple[str, ...]
    operations: BetaOperations | None = None
    reasons: tuple[str, ...] = ()
    evidence_scope: Literal["D0_SYNTHETIC_CONTRACT_ONLY"] = "D0_SYNTHETIC_CONTRACT_ONLY"
    actionable: Literal[False] = False
