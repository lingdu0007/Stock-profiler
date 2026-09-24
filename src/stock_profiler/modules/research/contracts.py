"""Frozen research, raw-score, risk-veto, and handoff contracts."""

from __future__ import annotations

import json
import re
from calendar import monthrange
from collections.abc import Callable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from hashlib import sha256
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictBool,
    model_validator,
)

RESEARCH_CONTRACT_VERSION = "1.0.0"
RESEARCH_DEFINITION_ID = "synthetic-monthly-research"
RESEARCH_LEGACY_DEFINITION_VERSION = "2.0.0"
RESEARCH_PRIOR_DEFINITION_VERSION = "3.0.0"
RESEARCH_DEFINITION_VERSION = "4.0.0"
RESEARCH_MODEL_ADAPTER_ID = "m-agent-deterministic-research-adapter"
RESEARCH_LEGACY_ROUTING_POLICY_VERSION = "monthly-research-risk-veto-v1"
RESEARCH_ROUTING_POLICY_VERSION = "monthly-research-risk-veto-v2"
RESEARCH_OUTPUT_CONTRACT_ID = "synthetic-monthly-research-draft"
RESEARCH_LEGACY_OUTPUT_CONTRACT_VERSION = "1.0.0"
RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION = "2.0.0"
RESEARCH_OUTPUT_CONTRACT_VERSION = "3.0.0"
RISK_DEFINITION_ID = "synthetic-independent-risk-veto"
RISK_DEFINITION_VERSION = "1.0.0"
RISK_MODEL_ADAPTER_ID = "m-agent-deterministic-risk-adapter"
RISK_OUTPUT_CONTRACT_ID = "synthetic-independent-risk-veto"
RISK_OUTPUT_CONTRACT_VERSION = "1.0.0"
RESEARCH_SCOPE: Literal["D0_SYNTHETIC_RESEARCH_ONLY"] = "D0_SYNTHETIC_RESEARCH_ONLY"
RESEARCH_LEGACY_ANNOUNCEMENT_TOOL_VERSION = "synthetic-announcement-tool-v1"
RESEARCH_ANNOUNCEMENT_TOOL_VERSION = "synthetic-announcement-tool-v2"
RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION: Literal["1.0.0"] = "1.0.0"
RESEARCH_EVIDENCE_CONTRACT_VERSION: Literal["2.0.0"] = "2.0.0"
LEGACY_RESEARCH_MISSING_TEXT = "HISTORICAL_CONTRACT_FIELD_NOT_RECORDED"
_LEGACY_RESEARCH_EVIDENCE_DECODING: ContextVar[bool] = ContextVar(
    "legacy_research_evidence_decoding",
    default=False,
)
_LEGACY_RESEARCH_COMMAND_DECODING: ContextVar[bool] = ContextVar(
    "legacy_research_command_decoding",
    default=False,
)
_RESEARCH_DEFINITION_VERSION_OVERRIDE: ContextVar[str | None] = ContextVar(
    "research_definition_version_override",
    default=None,
)
ResearchContractMode = Literal["current", "historical", "legacy"]
ResearchDataType = Literal[
    "DAILY_MARKET",
    "MONEY_FLOW",
    "INSTITUTIONAL_ACTIVITY",
    "FINANCIAL_STATEMENTS",
]
RESEARCH_REQUIRED_DATA_TYPES: tuple[ResearchDataType, ...] = (
    "DAILY_MARKET",
    "MONEY_FLOW",
    "INSTITUTIONAL_ACTIVITY",
    "FINANCIAL_STATEMENTS",
)

RAW_SCORE_MODEL_VERSION = "elastic-net-logistic-z20-v1"
RAW_SCORE_TARGET: Literal["SIX_MONTH_TERMINAL_20_PERCENT"] = "SIX_MONTH_TERMINAL_20_PERCENT"
RAW_SCORE_TRAINING_WINDOW_POLICY: Literal["EXPANDING_60_TO_119_ROLLING_120"] = (
    "EXPANDING_60_TO_119_ROLLING_120"
)
RAW_SCORE_LABEL_HORIZON_MONTHS = 6
RAW_SCORE_TRAINING_START_MONTH = "2036-12"
RAW_SCORE_PENALTY_STRENGTH = Decimal("1")
RAW_SCORE_INTERCEPT = Decimal("-0.40")
RAW_SCORE_L1_RATIO = Decimal("0.25")
RAW_SCORE_L2_RATIO = Decimal("0.75")
_RAW_SCORE_DECIMAL_CONTEXT = Context(prec=38)
_RAW_SCORE_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
RAW_SCORE_INTERACTION_TERMS: tuple[str, ...] = ()
RAW_SCORE_FEATURE_IDS: tuple[str, ...] = (
    "single_quarter_revenue_acceleration",
    "asset_normalized_quarter_profit_improvement",
    "operating_cash_flow_return_on_assets",
    "working_capital_pressure_change",
    "leverage_ratio_change",
    "industry_relative_return_20d",
    "downside_semivariance_60d",
    "max_drawdown_60d",
    "turnover_change",
    "institutional_net_buy_ratio",
    "institutional_listing_frequency",
)
RAW_SCORE_FEATURE_DATA_TYPES: dict[str, ResearchDataType] = {
    "single_quarter_revenue_acceleration": "FINANCIAL_STATEMENTS",
    "asset_normalized_quarter_profit_improvement": "FINANCIAL_STATEMENTS",
    "operating_cash_flow_return_on_assets": "FINANCIAL_STATEMENTS",
    "working_capital_pressure_change": "FINANCIAL_STATEMENTS",
    "leverage_ratio_change": "FINANCIAL_STATEMENTS",
    "industry_relative_return_20d": "DAILY_MARKET",
    "downside_semivariance_60d": "DAILY_MARKET",
    "max_drawdown_60d": "DAILY_MARKET",
    "turnover_change": "DAILY_MARKET",
    "institutional_net_buy_ratio": "INSTITUTIONAL_ACTIVITY",
    "institutional_listing_frequency": "INSTITUTIONAL_ACTIVITY",
}
RAW_SCORE_COEFFICIENTS: dict[str, Decimal] = {
    "screening_positive_prior": Decimal("0.15"),
    "screening_terminal_prior": Decimal("0.35"),
    "single_quarter_revenue_acceleration": Decimal("0.08"),
    "asset_normalized_quarter_profit_improvement": Decimal("0.07"),
    "operating_cash_flow_return_on_assets": Decimal("0.06"),
    "working_capital_pressure_change": Decimal("0.05"),
    "leverage_ratio_change": Decimal("0.04"),
    "industry_relative_return_20d": Decimal("0.09"),
    "downside_semivariance_60d": Decimal("0.03"),
    "max_drawdown_60d": Decimal("0.04"),
    "turnover_change": Decimal("0.02"),
    "institutional_net_buy_ratio": Decimal("0.05"),
    "institutional_listing_frequency": Decimal("0.06"),
}
RAW_SCORE_INPUT_IDS: tuple[str, ...] = (
    "screening_positive_prior",
    "screening_terminal_prior",
    *RAW_SCORE_FEATURE_IDS,
)
RAW_SCORE_UNCONSTRAINED_FEATURE_IDS = frozenset(
    {"turnover_change", "institutional_listing_frequency"}
)
RAW_SCORE_REVERSED_FEATURE_IDS = frozenset(
    {
        "working_capital_pressure_change",
        "leverage_ratio_change",
        "downside_semivariance_60d",
        "max_drawdown_60d",
    }
)


def research_contract_mode_for_versions(
    definition_version: str,
    output_contract_version: str,
) -> ResearchContractMode:
    """Select one explicit research compatibility boundary from persisted versions."""
    pair = (definition_version, output_contract_version)
    if pair == (
        RESEARCH_DEFINITION_VERSION,
        RESEARCH_OUTPUT_CONTRACT_VERSION,
    ):
        return "current"
    if pair == (
        RESEARCH_PRIOR_DEFINITION_VERSION,
        RESEARCH_PRIOR_OUTPUT_CONTRACT_VERSION,
    ):
        return "historical"
    if pair == (
        RESEARCH_LEGACY_DEFINITION_VERSION,
        RESEARCH_LEGACY_OUTPUT_CONTRACT_VERSION,
    ):
        return "legacy"
    raise ValueError("unsupported research Definition and output contract version pair")


def is_legacy_research_version_pair(
    definition_version: str,
    output_contract_version: str,
) -> bool:
    """Return whether a persisted research pair uses the pre-member-Run contract."""
    return (
        research_contract_mode_for_versions(definition_version, output_contract_version)
        == "legacy"
    )


def is_historical_research_version_pair(
    definition_version: str,
    output_contract_version: str,
) -> bool:
    """Return whether a persisted research pair uses the prior current contract."""
    return (
        research_contract_mode_for_versions(definition_version, output_contract_version)
        == "historical"
    )


def _raw_score_month_index(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:])


def _raw_score_add_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    return value.replace(
        year=year,
        month=month_zero + 1,
        day=min(value.day, monthrange(year, month_zero + 1)[1]),
    )


class RawScoreCalculationError(ValueError):
    """A structured raw-score calculation failed without a downstream result."""


class ResearchContract(BaseModel):
    """Reject unversioned research fields and mutable contract payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="never")


def _validate_evidence_clocks(
    *,
    source_published_at: AwareDatetime,
    acquired_at: AwareDatetime,
    validated_at: AwareDatetime,
    knowledge_cutoff: AwareDatetime,
) -> None:
    if source_published_at > acquired_at or acquired_at > validated_at:
        raise ValueError("research evidence clocks must be monotonic")
    if validated_at > knowledge_cutoff:
        raise ValueError("research evidence must be available by the knowledge cutoff")


class RawScoreFeatureTransform(ResearchContract):
    """Frozen winsorization and robust-standardization parameters for one signal."""

    lower_clip: Decimal
    upper_clip: Decimal
    median: Decimal
    iqr: Decimal
    reverse: bool

    @model_validator(mode="after")
    def validate_parameters(self) -> RawScoreFeatureTransform:
        parameters = (self.lower_clip, self.upper_clip, self.median, self.iqr)
        if any(not value.is_finite() for value in parameters):
            raise ValueError("raw-score feature transform parameters must be finite")
        if self.lower_clip >= self.upper_clip or not (
            self.lower_clip <= self.median <= self.upper_clip
        ):
            raise ValueError("raw-score feature transform clip bounds are invalid")
        if self.iqr <= 0:
            raise ValueError("raw-score feature transform IQR must be positive")
        return self


class RawScoreTrainingCohort(ResearchContract):
    """Frozen historical fixed-ten cohort and its successful research members."""

    cohort_id: str = Field(min_length=1)
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    member_security_ids: tuple[str, ...] = Field(min_length=10, max_length=10)
    completed_research_ids: dict[str, str] = Field(min_length=1)
    research_definition_id: str = Field(min_length=1)
    research_definition_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_members(self) -> RawScoreTrainingCohort:
        if (
            self.research_definition_id != RESEARCH_DEFINITION_ID
            or self.research_definition_version
            not in {
                RESEARCH_LEGACY_DEFINITION_VERSION,
                RESEARCH_PRIOR_DEFINITION_VERSION,
                RESEARCH_DEFINITION_VERSION,
            }
        ):
            raise ValueError("raw-score training cohort must use the supported research Definition")
        if len(set(self.member_security_ids)) != len(self.member_security_ids):
            raise ValueError("frozen training cohort member identities must be unique")
        if not set(self.completed_research_ids).issubset(self.member_security_ids):
            raise ValueError("completed research members must belong to the frozen cohort")
        if len(set(self.completed_research_ids.values())) != len(self.completed_research_ids):
            raise ValueError("completed research identities must be unique")
        return self


class RawScoreTrainingRecord(ResearchContract):
    """Frozen evidence that one historical raw-score label is eligible."""

    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    cohort_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    selection_cutoff_at: AwareDatetime
    evaluation_entry_at: AwareDatetime
    terminal_label: StrictBool
    label_available_at: AwareDatetime
    source_model_version: str = Field(min_length=1)


class RawScoreModelSnapshot(ResearchContract):
    """Frozen synthetic model metadata and parameter snapshot for uncalibrated z20."""

    algorithm: Literal["ELASTIC_NET_LOGISTIC"]
    model_version: str = Field(min_length=1)
    target: Literal["SIX_MONTH_TERMINAL_20_PERCENT"]
    training_window_id: str = Field(min_length=1)
    training_window_policy: Literal["EXPANDING_60_TO_119_ROLLING_120"]
    training_window_kind: Literal["EXPANDING", "ROLLING_120"]
    training_window_month_count: int = Field(ge=1, le=120)
    training_window_start_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    training_window_end_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    training_months: tuple[str, ...] = Field(min_length=1)
    label_watermark_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    label_watermark_at: AwareDatetime
    training_cohorts: tuple[RawScoreTrainingCohort, ...] = Field(min_length=1)
    training_records: tuple[RawScoreTrainingRecord, ...] = Field(min_length=1)
    normalization_snapshot_id: str = Field(min_length=1)
    mature_months: int = Field(ge=0)
    training_record_count: int = Field(ge=0)
    positive_record_count: int = Field(ge=0)
    negative_record_count: int = Field(ge=0)
    intercept: Decimal
    coefficients: dict[str, Decimal]
    transformations: dict[str, RawScoreFeatureTransform]
    interaction_terms: tuple[str, ...] = ()
    l1_ratio: Decimal
    l2_ratio: Decimal
    penalty_strength: Decimal

    @model_validator(mode="after")
    def validate_training_snapshot(self) -> RawScoreModelSnapshot:
        legacy_decoding = _LEGACY_RESEARCH_COMMAND_DECODING.get()
        if self.l1_ratio != RAW_SCORE_L1_RATIO or self.l2_ratio != RAW_SCORE_L2_RATIO:
            raise ValueError("raw-score Elastic Net must use 25% L1 and 75% L2")
        if self.model_version != RAW_SCORE_MODEL_VERSION:
            raise ValueError("unsupported raw-score model version")
        if self.penalty_strength != RAW_SCORE_PENALTY_STRENGTH:
            raise ValueError("raw-score penalty strength is frozen for this model version")
        if self.training_window_policy != RAW_SCORE_TRAINING_WINDOW_POLICY:
            raise ValueError("unsupported raw-score training window policy")
        if any(not _RAW_SCORE_MONTH_PATTERN.fullmatch(month) for month in self.training_months):
            raise ValueError("raw-score training months must use YYYY-MM")
        if self.training_window_month_count != len(self.training_months):
            raise ValueError("raw-score training window month count does not match months")
        month_indexes = tuple(_raw_score_month_index(month) for month in self.training_months)
        if any(
            current_month != previous_month + 1
            for previous_month, current_month in zip(month_indexes, month_indexes[1:], strict=False)
        ):
            raise ValueError("raw-score training months must be consecutive")
        if (
            self.training_months[0] != self.training_window_start_month
            or self.training_months[-1] != self.training_window_end_month
        ):
            raise ValueError("raw-score training window dates do not match the months")
        if not legacy_decoding:
            if self.label_watermark_at is None:
                raise ValueError("raw-score label watermark date is required")
            if self.label_watermark_at.strftime("%Y-%m") != self.label_watermark_month:
                raise ValueError("raw-score label watermark date does not match its month")
            if not self.training_cohorts or not self.training_records:
                raise ValueError("raw-score training provenance is required")
            security_month_keys = [
                (record.month, record.security_id) for record in self.training_records
            ]
            if len(set(security_month_keys)) != len(security_month_keys):
                raise ValueError(
                    "raw-score training records must have unique security-month evidence"
                )
            cohorts_by_id = {cohort.cohort_id: cohort for cohort in self.training_cohorts}
            if len(cohorts_by_id) != len(self.training_cohorts):
                raise ValueError("raw-score training cohorts must have unique identities")
            if {cohort.month for cohort in self.training_cohorts} != set(self.training_months):
                raise ValueError("raw-score training cohorts must cover every training month")
            if len({cohort.month for cohort in self.training_cohorts}) != len(
                self.training_cohorts
            ):
                raise ValueError("raw-score training cohorts must have one cohort per month")
            if len({cohort.research_definition_version for cohort in self.training_cohorts}) != 1:
                raise ValueError("raw-score training cohorts must use the same research Definition")
            records_by_cohort: dict[str, list[RawScoreTrainingRecord]] = {}
            for record in self.training_records:
                cohort = cohorts_by_id.get(record.cohort_id)
                if cohort is None:
                    raise ValueError("raw-score training record must bind to a frozen cohort")
                if record.month != cohort.month:
                    raise ValueError(
                        "raw-score training record month must match its frozen cohort"
                    )
                if record.security_id not in cohort.completed_research_ids:
                    raise ValueError(
                        "raw-score training record must bind to a frozen cohort member"
                    )
                if cohort.completed_research_ids[record.security_id] != record.research_id:
                    raise ValueError("raw-score training record research identity is not frozen")
                records_by_cohort.setdefault(record.cohort_id, []).append(record)
            for cohort in self.training_cohorts:
                actual_members = {
                    record.security_id for record in records_by_cohort.get(cohort.cohort_id, ())
                }
                if actual_members != set(cohort.completed_research_ids):
                    raise ValueError(
                        "raw-score training records must include every successful "
                        "frozen research member"
                    )
            record_keys = [
                (record.month, record.security_id, record.research_id)
                for record in self.training_records
            ]
            if len(set(record_keys)) != len(record_keys):
                raise ValueError("raw-score training records must be unique")
            if {record.month for record in self.training_records} != set(self.training_months):
                raise ValueError("raw-score training records must cover every training month")
            if self.mature_months != len(set(record.month for record in self.training_records)):
                raise ValueError("raw-score mature month count must match training records")
            if self.training_record_count != len(self.training_records):
                raise ValueError("raw-score training record count does not match records")
            if (
                self.positive_record_count + self.negative_record_count
                != self.training_record_count
            ):
                raise ValueError("raw-score training records must equal the two class counts")
            positive_records = sum(record.terminal_label for record in self.training_records)
            if self.positive_record_count != positive_records:
                raise ValueError("raw-score positive record count does not match records")
            if self.negative_record_count != self.training_record_count - positive_records:
                raise ValueError("raw-score negative record count does not match records")
            if any(
                record.source_model_version != self.model_version
                for record in self.training_records
            ):
                raise ValueError("raw-score training records must use the frozen model version")
            if any(
                record.selection_cutoff_at.strftime("%Y-%m") != record.month
                for record in self.training_records
            ):
                raise ValueError("raw-score training record cutoff does not match its month")
            if any(
                record.evaluation_entry_at <= record.selection_cutoff_at
                for record in self.training_records
            ):
                raise ValueError("raw-score evaluation entry must follow the selection cutoff")
            if any(record.evaluation_entry_at.weekday() >= 5 for record in self.training_records):
                raise ValueError("raw-score evaluation entry must be a synthetic trading day")
            if any(
                _raw_score_label_available_at(record.evaluation_entry_at)
                > record.label_available_at
                for record in self.training_records
            ):
                raise ValueError("raw-score training record label maturity is incomplete")
            if any(
                self.label_watermark_at is None
                or record.label_available_at > self.label_watermark_at
                for record in self.training_records
            ):
                raise ValueError(
                    "raw-score training record exceeds the label availability watermark"
                )
            if max(record.label_available_at for record in self.training_records) != (
                self.label_watermark_at
            ):
                raise ValueError("raw-score label watermark must match the latest training record")
        if self.interaction_terms:
            raise ValueError("raw-score model snapshot must not contain interaction terms")
        if set(self.coefficients) != set(RAW_SCORE_INPUT_IDS):
            raise ValueError("raw-score model snapshot must cover the registered inputs")
        if set(self.transformations) != set(RAW_SCORE_FEATURE_IDS):
            raise ValueError("raw-score model snapshot must cover every feature transform")
        if {
            feature_id
            for feature_id, transform in self.transformations.items()
            if transform.reverse
        } != set(RAW_SCORE_REVERSED_FEATURE_IDS):
            raise ValueError(
                "raw-score feature transform directions do not match the frozen version"
            )
        if any(
            not value.is_finite() or (value < 0 and key not in RAW_SCORE_UNCONSTRAINED_FEATURE_IDS)
            for key, value in self.coefficients.items()
        ):
            raise ValueError(
                "raw-score coefficients must be finite and non-negative "
                "except for unconstrained features"
            )
        if not self.intercept.is_finite():
            raise ValueError("raw-score intercept must be finite")
        if self.has_sufficient_training_evidence:
            if self.mature_months >= 120:
                if (
                    self.training_window_kind != "ROLLING_120"
                    or self.training_window_month_count != 120
                ):
                    raise ValueError(
                        "raw-score mature window must use the 120-month rolling policy"
                    )
            elif (
                self.training_window_kind != "EXPANDING"
                or self.training_window_month_count != self.mature_months
            ):
                raise ValueError("raw-score mature window must use the expanding policy")
        return self

    @property
    def has_sufficient_training_evidence(self) -> bool:
        """Whether this frozen model is allowed to produce a new sample-out score."""
        return (
            self.mature_months >= 60
            and self.training_record_count >= 500
            and self.positive_record_count >= 50
            and self.negative_record_count >= 50
        )


class LegacyRawScoreModelSnapshot(RawScoreModelSnapshot):
    """Compatibility view for the original model snapshot without provenance additions."""

    label_watermark_at: AwareDatetime | None = None  # type: ignore[assignment]
    training_cohorts: tuple[RawScoreTrainingCohort, ...] = ()
    training_records: tuple[RawScoreTrainingRecord, ...] = ()


def _training_month_sequence(start_year: int, start_month: int, count: int) -> tuple[str, ...]:
    start_index = start_year * 12 + start_month - 1
    return tuple(
        f"{month_index // 12:04}-{month_index % 12 + 1:02}"
        for month_index in range(start_index, start_index + count)
    )


def _raw_score_month_end(month: str) -> datetime:
    year, month_number = (int(part) for part in month.split("-"))
    return datetime(
        year,
        month_number,
        monthrange(year, month_number)[1],
        23,
        59,
        59,
        tzinfo=UTC,
    )


def _raw_score_evaluation_entry_at(selection_cutoff_at: datetime) -> datetime:
    """Return the first synthetic weekday session after the frozen cutoff."""
    candidate = selection_cutoff_at.replace(
        hour=16,
        minute=0,
        second=0,
        microsecond=0,
    ) + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _raw_score_label_available_at(evaluation_entry_at: datetime) -> datetime:
    """Apply the six-month horizon and roll a weekend target back to Friday."""
    candidate = _raw_score_add_months(evaluation_entry_at, RAW_SCORE_LABEL_HORIZON_MONTHS)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _raw_score_mature_training_months(cutoff_at: datetime) -> tuple[str, ...]:
    """Return every fixed-inception month whose terminal label is mature by the cutoff."""
    start_year, start_month = (int(part) for part in RAW_SCORE_TRAINING_START_MONTH.split("-"))
    mature_months: list[str] = []
    for month in _training_month_sequence(start_year, start_month, 1200):
        selection_cutoff_at = _raw_score_month_end(month)
        evaluation_entry_at = _raw_score_evaluation_entry_at(selection_cutoff_at)
        if _raw_score_label_available_at(evaluation_entry_at) > cutoff_at:
            break
        mature_months.append(month)
    return tuple(mature_months)


def _frozen_raw_score_training_records(
    training_months: tuple[str, ...],
) -> tuple[tuple[RawScoreTrainingCohort, ...], tuple[RawScoreTrainingRecord, ...]]:
    cohorts: list[RawScoreTrainingCohort] = []
    records: list[RawScoreTrainingRecord] = []
    record_index = 0
    for month_index, month in enumerate(training_months):
        record_count = 9 if month_index < 20 else 8
        selection_cutoff_at = _raw_score_month_end(month)
        evaluation_entry_at = _raw_score_evaluation_entry_at(selection_cutoff_at)
        label_available_at = _raw_score_label_available_at(evaluation_entry_at)
        cohort_id = f"synthetic-training-cohort-{month}"
        member_security_ids = tuple(
            f"synthetic-training-security-{month_index * 10 + member_index:04}"
            for member_index in range(10)
        )
        completed_research_ids = {
            security_id: f"synthetic-training-research-{month_index * 10 + member_index:04}"
            for member_index, security_id in enumerate(member_security_ids[:record_count])
        }
        cohorts.append(
            RawScoreTrainingCohort(
                cohort_id=cohort_id,
                month=month,
                member_security_ids=member_security_ids,
                completed_research_ids=completed_research_ids,
                research_definition_id=RESEARCH_DEFINITION_ID,
                research_definition_version=RESEARCH_DEFINITION_VERSION,
            )
        )
        for security_id, research_id in completed_research_ids.items():
            records.append(
                RawScoreTrainingRecord(
                    month=month,
                    cohort_id=cohort_id,
                    security_id=security_id,
                    research_id=research_id,
                    selection_cutoff_at=selection_cutoff_at,
                    evaluation_entry_at=evaluation_entry_at,
                    terminal_label=record_index % 2 == 0,
                    label_available_at=label_available_at,
                    source_model_version=RAW_SCORE_MODEL_VERSION,
                )
            )
            record_index += 1
    return tuple(cohorts), tuple(records)


def frozen_raw_score_model_snapshot() -> RawScoreModelSnapshot:
    """Return the deterministic D0 model artifact without claiming live training."""
    start_year, start_month = (int(part) for part in RAW_SCORE_TRAINING_START_MONTH.split("-"))
    training_months = _training_month_sequence(start_year, start_month, 60)
    training_cohorts, training_records = _frozen_raw_score_training_records(training_months)
    label_watermark_at = max(record.label_available_at for record in training_records)
    return RawScoreModelSnapshot(
        algorithm="ELASTIC_NET_LOGISTIC",
        model_version=RAW_SCORE_MODEL_VERSION,
        target=RAW_SCORE_TARGET,
        training_window_id="synthetic-training-window-expanding-60m",
        training_window_policy=RAW_SCORE_TRAINING_WINDOW_POLICY,
        training_window_kind="EXPANDING",
        training_window_month_count=60,
        training_window_start_month=RAW_SCORE_TRAINING_START_MONTH,
        training_window_end_month="2041-11",
        training_months=training_months,
        label_watermark_month=label_watermark_at.strftime("%Y-%m"),
        label_watermark_at=label_watermark_at,
        training_cohorts=training_cohorts,
        training_records=training_records,
        normalization_snapshot_id="synthetic-normalization-v1",
        mature_months=60,
        training_record_count=500,
        positive_record_count=250,
        negative_record_count=250,
        intercept=RAW_SCORE_INTERCEPT,
        coefficients=dict(RAW_SCORE_COEFFICIENTS),
        transformations={
            feature_id: RawScoreFeatureTransform(
                lower_clip=Decimal("-3"),
                upper_clip=Decimal("3"),
                median=Decimal("0"),
                iqr=Decimal("1"),
                reverse=feature_id in RAW_SCORE_REVERSED_FEATURE_IDS,
            )
            for feature_id in RAW_SCORE_FEATURE_IDS
        },
        interaction_terms=RAW_SCORE_INTERACTION_TERMS,
        l1_ratio=RAW_SCORE_L1_RATIO,
        l2_ratio=RAW_SCORE_L2_RATIO,
        penalty_strength=RAW_SCORE_PENALTY_STRENGTH,
    )


class ResearchEvidence(ResearchContract):
    evidence_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_contract_version: Literal["1.0.0", "2.0.0"] = RESEARCH_EVIDENCE_CONTRACT_VERSION
    effective_at: AwareDatetime | None = None
    source_published_at: AwareDatetime | None = None
    acquired_at: AwareDatetime | None = None
    validated_at: AwareDatetime | None = None
    knowledge_cutoff: AwareDatetime
    semantic_version: str = Field(min_length=1)
    validation_status: Literal["VALIDATED"]
    _legacy_decoded: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def validate_clocks(self) -> ResearchEvidence:
        if (
            self.evidence_contract_version == RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION
            and not _LEGACY_RESEARCH_EVIDENCE_DECODING.get()
            and not self._legacy_decoded
        ):
            raise ValueError("legacy research evidence requires a historical case boundary")
        if self.evidence_contract_version == RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION:
            if self.acquired_at is not None and self.validated_at is not None and (
                self.acquired_at > self.validated_at
                or self.validated_at > self.knowledge_cutoff
            ):
                raise ValueError("research evidence must be available by the knowledge cutoff")
            return self
        if (
            self.effective_at is None
            or self.source_published_at is None
            or self.acquired_at is None
            or self.validated_at is None
        ):
            raise ValueError("current research evidence requires all evidence clocks")
        _validate_evidence_clocks(
            source_published_at=self.source_published_at,
            acquired_at=self.acquired_at,
            validated_at=self.validated_at,
            knowledge_cutoff=self.knowledge_cutoff,
        )
        return self


class ResearchToolEvidence(ResearchContract):
    """Structured provenance emitted by the allowlisted exploratory Tool."""

    evidence_id: str = Field(pattern=r"^announcement:.+")
    source: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    evidence_contract_version: Literal["1.0.0", "2.0.0"] = RESEARCH_EVIDENCE_CONTRACT_VERSION
    effective_at: AwareDatetime | None = None
    source_published_at: AwareDatetime | None = None
    acquired_at: AwareDatetime | None = None
    validated_at: AwareDatetime | None = None
    knowledge_cutoff: AwareDatetime
    semantic_version: str = Field(min_length=1)
    validation_status: Literal["VALIDATED"]
    _legacy_decoded: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def validate_clocks(self) -> ResearchToolEvidence:
        if (
            self.evidence_contract_version == RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION
            and not _LEGACY_RESEARCH_EVIDENCE_DECODING.get()
            and not self._legacy_decoded
        ):
            raise ValueError("legacy research Tool evidence requires a historical case boundary")
        if self.evidence_contract_version == RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION:
            if self.acquired_at is not None and self.validated_at is not None and (
                self.acquired_at > self.validated_at
                or self.validated_at > self.knowledge_cutoff
            ):
                raise ValueError("research Tool evidence must be available by the knowledge cutoff")
            return self
        if (
            self.effective_at is None
            or self.source_published_at is None
            or self.acquired_at is None
            or self.validated_at is None
        ):
            raise ValueError("current research Tool evidence requires all evidence clocks")
        _validate_evidence_clocks(
            source_published_at=self.source_published_at,
            acquired_at=self.acquired_at,
            validated_at=self.validated_at,
            knowledge_cutoff=self.knowledge_cutoff,
        )
        return self


def research_evidence_payload(
    evidence: ResearchEvidence | ResearchToolEvidence,
    *,
    legacy: bool,
) -> dict[str, object]:
    """Serialize current or historical evidence without weakening direct decoders."""
    payload = evidence.model_dump(mode="json", exclude_none=legacy)
    if legacy:
        payload.pop("evidence_contract_version", None)
    return payload


class FrozenDualTargetScreening(ResearchContract):
    """The full-domain, two-head screening output handed to fixed-ten research."""

    positive_target: Literal["SIX_MONTH_POSITIVE_RETURN"]
    terminal_target: Literal["SIX_MONTH_TERMINAL_20_PERCENT"]
    strategy_version: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    universe_security_ids: tuple[str, ...] = Field(min_length=10)
    selected_member_ids: tuple[str, ...] = Field(min_length=10, max_length=10)
    positive_scores: dict[str, Decimal]
    terminal_scores: dict[str, Decimal]
    positive_percentiles: dict[str, Decimal]
    terminal_percentiles: dict[str, Decimal]
    positive_head_version: str = Field(min_length=1)
    terminal_head_version: str = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_dual_head_output(self) -> FrozenDualTargetScreening:
        universe_ids = set(self.universe_security_ids)
        selected_ids = set(self.selected_member_ids)
        if len(universe_ids) != len(self.universe_security_ids):
            raise ValueError("screening universe security identities must be unique")
        if len(selected_ids) != 10:
            raise ValueError("screening output must contain exactly ten selected members")
        if not selected_ids.issubset(universe_ids):
            raise ValueError("selected screening members must belong to the full domain")
        if (
            set(self.positive_scores) != universe_ids
            or set(self.terminal_scores) != universe_ids
            or set(self.positive_percentiles) != universe_ids
            or set(self.terminal_percentiles) != universe_ids
        ):
            raise ValueError("screening targets and percentiles must cover the full domain")
        if any(
            not value.is_finite()
            for value in (
                *self.positive_scores.values(),
                *self.terminal_scores.values(),
                *self.positive_percentiles.values(),
                *self.terminal_percentiles.values(),
            )
        ):
            raise ValueError("screening scores and percentiles must be finite")
        if any(
            value < 0 or value > 100
            for value in (*self.positive_percentiles.values(), *self.terminal_percentiles.values())
        ):
            raise ValueError("screening percentiles must be between zero and one hundred")
        if self.output_sha256 != screening_output_sha256(self):
            raise ValueError("screening output hash does not match canonical output")
        return self


class ResearchDataManifestEntry(ResearchContract):
    """One complete, cutoff-bound Provider contract for a member."""

    data_type: ResearchDataType
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    completeness: Literal["COMPLETE", "INCOMPLETE"]
    event_status: Literal["PRESENT", "VERIFIED_EMPTY", "UNAVAILABLE"]
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    knowledge_cutoff: AwareDatetime

    @model_validator(mode="after")
    def validate_availability(self) -> ResearchDataManifestEntry:
        if self.completeness == "INCOMPLETE" and self.event_status != "UNAVAILABLE":
            raise ValueError("incomplete research data manifest entries must be unavailable")
        if self.completeness == "COMPLETE" and self.event_status == "UNAVAILABLE":
            raise ValueError("complete research data manifest entries cannot be unavailable")
        if self.event_status == "VERIFIED_EMPTY" and self.data_type != "INSTITUTIONAL_ACTIVITY":
            raise ValueError("only institutional activity may use a verified-empty event status")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("research data manifest evidence identities must be unique")
        return self


class ResearchDataManifest(ResearchContract):
    """The per-stock required-data availability contract delivered by a Provider."""

    version: str = Field(min_length=1)
    entries: tuple[ResearchDataManifestEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_required_data_types(self) -> ResearchDataManifest:
        actual = tuple(entry.data_type for entry in self.entries)
        if actual != RESEARCH_REQUIRED_DATA_TYPES:
            raise ValueError(
                "research data manifest must contain the required research data types exactly once"
            )
        return self


def _validate_research_stage_source(
    stage_id: str,
    source_stage_id: str,
    bull_case: str | None,
    bear_case: str | None,
) -> None:
    expected_source = {
        "analyze": "collect",
        "bull-bear": "analyze",
        "draft": "bull-bear",
    }[stage_id]
    if source_stage_id != expected_source:
        raise ValueError("research stage artifact source does not match its stage")
    if stage_id in {"bull-bear", "draft"} and (not bull_case or not bear_case):
        raise ValueError("research debate artifacts require both bull and bear cases")


class LegacyResearchStageArtifact(ResearchContract):
    """The original staged artifact shape without checkpoint input identities."""

    stage_id: Literal["analyze", "bull-bear", "draft"]
    source_stage_id: Literal["collect", "analyze", "bull-bear"]
    security_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    summary: str = Field(min_length=1)
    bull_case: str | None = None
    bear_case: str | None = None

    @model_validator(mode="after")
    def validate_stage_progression(self) -> LegacyResearchStageArtifact:
        _validate_research_stage_source(
            self.stage_id,
            self.source_stage_id,
            self.bull_case,
            self.bear_case,
        )
        return self


class ResearchStageArtifact(ResearchContract):
    """Typed intermediate evidence passed between staged research phases."""

    stage_id: Literal["analyze", "bull-bear", "draft"]
    source_stage_id: Literal["collect", "analyze", "bull-bear"]
    input_item_ids: tuple[str, ...] = Field(min_length=1)
    security_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    summary: str = Field(min_length=1)
    bull_case: str | None = None
    bear_case: str | None = None
    catalysts: tuple[str, ...] = ()
    falsification_conditions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_stage_progression(self) -> ResearchStageArtifact:
        _validate_research_stage_source(
            self.stage_id,
            self.source_stage_id,
            self.bull_case,
            self.bear_case,
        )
        if len(set(self.input_item_ids)) != len(self.input_item_ids):
            raise ValueError("research stage artifact input identities must be unique")
        if self.stage_id == "draft" and (
            not self.catalysts
            or not self.falsification_conditions
            or not self.unknowns
        ):
            raise ValueError(
                "research draft artifacts require catalysts, falsification, and unknowns"
            )
        return self


def decode_legacy_research_stage_artifact(value: object) -> ResearchStageArtifact:
    """Decode a pre-v1.0.0 stage artifact without rewriting its stored payload."""
    if not isinstance(value, dict):
        raise ValueError("legacy research stage artifact must be an object")
    payload = deepcopy(value)
    if "input_item_ids" not in payload:
        legacy_artifact = LegacyResearchStageArtifact.model_validate(payload)
        normalized_payload = legacy_artifact.model_dump(mode="python")
        normalized_payload["input_item_ids"] = ()
        if legacy_artifact.stage_id == "draft":
            normalized_payload.update(
                catalysts=(LEGACY_RESEARCH_MISSING_TEXT,),
                falsification_conditions=(LEGACY_RESEARCH_MISSING_TEXT,),
                unknowns=(LEGACY_RESEARCH_MISSING_TEXT,),
            )
        else:
            normalized_payload.update(
                catalysts=(),
                falsification_conditions=(),
                unknowns=(),
            )
        return ResearchStageArtifact.model_construct(**normalized_payload)
    if payload.get("stage_id") == "draft":
        payload.setdefault("catalysts", (LEGACY_RESEARCH_MISSING_TEXT,))
        payload.setdefault("falsification_conditions", (LEGACY_RESEARCH_MISSING_TEXT,))
        payload.setdefault("unknowns", (LEGACY_RESEARCH_MISSING_TEXT,))
    else:
        payload.setdefault("catalysts", ())
        payload.setdefault("falsification_conditions", ())
        payload.setdefault("unknowns", ())
    return ResearchStageArtifact.model_validate(payload)


def research_stage_artifact_payload(
    artifact: ResearchStageArtifact,
    *,
    legacy: bool = False,
    historical_without_debate_fields: bool = False,
) -> dict[str, object]:
    """Serialize a stage artifact using the exact current or historical field set."""
    payload = artifact.model_dump(mode="json")
    if legacy:
        payload.pop("input_item_ids", None)
    if legacy or historical_without_debate_fields:
        payload.pop("catalysts", None)
        payload.pop("falsification_conditions", None)
        payload.pop("unknowns", None)
    return payload


class ResearchStructuredFacts(ResearchContract):
    """Cutoff-bound source facts from which raw-score signals are calculated."""

    revenue_growth_current: Decimal | None
    revenue_growth_prior: Decimal | None
    quarter_profit_improvement: Decimal | None
    average_total_assets: Decimal | None
    operating_cash_flow_ttm: Decimal | None
    working_capital_pressure_current: Decimal | None
    working_capital_pressure_prior: Decimal | None
    leverage_ratio_current: Decimal | None
    leverage_ratio_prior: Decimal | None
    stock_return_20d: Decimal | None
    industry_return_20d: Decimal | None
    downside_semivariance_60d: Decimal | None
    max_drawdown_60d: Decimal | None
    turnover_change: Decimal | None
    institutional_net_buy_ratio: Decimal | None
    institutional_listing_frequency: Decimal | None

    @model_validator(mode="after")
    def validate_facts(self) -> ResearchStructuredFacts:
        values = self.model_dump(mode="python").values()
        if any(value is not None and not value.is_finite() for value in values):
            raise ValueError("structured source facts must be finite")
        return self


def _structured_difference(
    current: Decimal | None,
    prior: Decimal | None,
) -> Decimal | None:
    if current is None or prior is None:
        return None
    return current - prior


def _structured_ratio(
    numerator: Decimal | None,
    denominator: Decimal | None,
) -> Decimal | None:
    if numerator is None or denominator is None:
        return None
    if denominator <= 0:
        raise ValueError("structured source facts require positive average total assets")
    return numerator / denominator


def _structured_signal_calculators(
    facts: ResearchStructuredFacts,
) -> dict[str, Callable[[], Decimal | None]]:
    """Define each signal formula once for strict and failure-tolerant evaluation."""
    return {
        "single_quarter_revenue_acceleration": lambda: _structured_difference(
            facts.revenue_growth_current,
            facts.revenue_growth_prior,
        ),
        "asset_normalized_quarter_profit_improvement": lambda: _structured_ratio(
            facts.quarter_profit_improvement,
            facts.average_total_assets,
        ),
        "operating_cash_flow_return_on_assets": lambda: _structured_ratio(
            facts.operating_cash_flow_ttm,
            facts.average_total_assets,
        ),
        "working_capital_pressure_change": lambda: _structured_ratio(
            _structured_difference(
                facts.working_capital_pressure_current,
                facts.working_capital_pressure_prior,
            ),
            facts.average_total_assets,
        ),
        "leverage_ratio_change": lambda: _structured_difference(
            facts.leverage_ratio_current,
            facts.leverage_ratio_prior,
        ),
        "industry_relative_return_20d": lambda: _structured_difference(
            facts.stock_return_20d,
            facts.industry_return_20d,
        ),
        "downside_semivariance_60d": lambda: facts.downside_semivariance_60d,
        "max_drawdown_60d": lambda: facts.max_drawdown_60d,
        "turnover_change": lambda: facts.turnover_change,
        "institutional_net_buy_ratio": lambda: facts.institutional_net_buy_ratio,
        "institutional_listing_frequency": lambda: facts.institutional_listing_frequency,
    }


def calculate_structured_signals(
    facts: ResearchStructuredFacts,
) -> dict[str, Decimal | None]:
    """Calculate the frozen raw-score signals from named source facts."""
    with localcontext(_RAW_SCORE_DECIMAL_CONTEXT):
        return {
            signal_id: calculate()
            for signal_id, calculate in _structured_signal_calculators(facts).items()
        }


def _calculate_structured_signals_with_failures(
    facts: ResearchStructuredFacts,
) -> dict[str, Decimal | None]:
    """Keep valid signals while marking only the formulas that failed."""

    def safe(calculate: Callable[[], Decimal | None]) -> Decimal | None:
        try:
            return calculate()
        except (ValueError, ArithmeticError):
            return None

    with localcontext(_RAW_SCORE_DECIMAL_CONTEXT):
        return {
            signal_id: safe(calculate)
            for signal_id, calculate in _structured_signal_calculators(facts).items()
        }


class ResearchMemberInput(ResearchContract):
    """Structured, cutoff-bound facts for one member of the research cohort."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    knowledge_cutoff: AwareDatetime
    evidence: tuple[ResearchEvidence, ...] = Field(min_length=1)
    data_manifest: ResearchDataManifest
    structured_facts: ResearchStructuredFacts
    structured_signals: dict[str, Decimal | None] = Field(default_factory=dict)
    risk_flags: tuple[str, ...] = ()
    _legacy_decoded: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def validate_member_facts(self) -> ResearchMemberInput:
        calculation_failed = False
        try:
            derived_signals = calculate_structured_signals(self.structured_facts)
        except (ValueError, ArithmeticError):
            calculation_failed = True
            derived_signals = _calculate_structured_signals_with_failures(self.structured_facts)
        if (
            self.structured_signals
            and self.structured_signals != derived_signals
            and not calculation_failed
        ):
            raise ValueError("structured signals must match the deterministic feature calculator")
        object.__setattr__(self, "structured_signals", derived_signals)
        evidence_ids = [evidence.evidence_id for evidence in self.evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("research evidence identities must be unique per member")
        manifest_evidence_ids = tuple(
            evidence_id
            for entry in self.data_manifest.entries
            for evidence_id in entry.evidence_ids
        )
        if set(manifest_evidence_ids) != set(evidence_ids):
            raise ValueError(
                "research data manifest must account for every Provider evidence identity"
            )
        if len(set(manifest_evidence_ids)) != len(manifest_evidence_ids):
            raise ValueError("research data manifest evidence identities must be unique")
        if any(
            entry.knowledge_cutoff != self.knowledge_cutoff for entry in self.data_manifest.entries
        ):
            raise ValueError("research data manifest and member knowledge cutoffs must agree")
        if any(evidence.knowledge_cutoff != self.knowledge_cutoff for evidence in self.evidence):
            raise ValueError("research evidence and member knowledge cutoffs must agree")
        if any(
            entry.completeness == "COMPLETE" and entry.event_status == "VERIFIED_EMPTY"
            for entry in self.data_manifest.entries
        ) and any(
            derived_signals[signal_id] != Decimal("0")
            for signal_id in (
                "institutional_net_buy_ratio",
                "institutional_listing_frequency",
            )
        ):
            raise ValueError(
                "verified-empty institutional activity requires zero institutional signals"
            )
        if any(
            evidence.evidence_contract_version not in {
                RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
                RESEARCH_EVIDENCE_CONTRACT_VERSION,
            }
            or evidence.validation_status != "VALIDATED"
            or (
                evidence.evidence_contract_version == RESEARCH_EVIDENCE_CONTRACT_VERSION
                and (
                    evidence.acquired_at is None
                    or evidence.validated_at is None
                    or evidence.acquired_at > evidence.validated_at
                    or evidence.validated_at > self.knowledge_cutoff
                )
            )
            for evidence in self.evidence
        ):
            raise ValueError("research evidence must be validated and available by the cutoff")
        if (
            any(
                evidence.evidence_contract_version != RESEARCH_EVIDENCE_CONTRACT_VERSION
                for evidence in self.evidence
            )
            and not _LEGACY_RESEARCH_EVIDENCE_DECODING.get()
            and not self._legacy_decoded
        ):
            raise ValueError("current research members require the current evidence contract")
        return self


class ResearchMemberHandoff(ResearchContract):
    """Immutable per-member evidence and risk facts consumed by risk."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    evidence: tuple[ResearchEvidence, ...] = Field(min_length=1)
    risk_flags: tuple[str, ...] = ()
    research_run_id: str | None = None


class ResearchCommand(ResearchContract):
    """The frozen host input for one fixed-ten monthly research run."""

    contract_version: Literal["1.0.0"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    selection_object_id: str = Field(min_length=1)
    selection_event_id: str = Field(min_length=1)
    selection_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cutoff_at: AwareDatetime
    knowledge_cutoff: AwareDatetime
    purpose: Literal["SYNTHETIC"]
    screening: FrozenDualTargetScreening
    members: tuple[ResearchMemberInput, ...]
    raw_score_model: RawScoreModelSnapshot
    failure_mode: Literal["NONE", "DATA", "RESEARCH", "RAW_SCORE", "RISK", "SYSTEM"] = "NONE"
    risk_scenario: Literal["ACCEPT", "REJECT"] = "ACCEPT"
    risk_rejected_member_ids: tuple[str, ...] = ()
    _legacy_decoded: bool = PrivateAttr(default=False)
    _historical_decoded: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def validate_fixed_ten_contract(self) -> ResearchCommand:
        if len(self.members) != 10:
            raise ValueError("research cohort must contain exactly ten members")
        security_ids = [member.security_id for member in self.members]
        research_ids = [member.research_id for member in self.members]
        if len(set(security_ids)) != 10:
            raise ValueError("research cohort security identities must be unique")
        if len(set(research_ids)) != 10:
            raise ValueError("research identities must be independent and unique")
        if self.cutoff_at != self.knowledge_cutoff:
            raise ValueError("research cutoff and knowledge cutoff must agree")
        if self.selection_fingerprint != selection_binding_sha256(
            self.selection_object_id,
            self.selection_event_id,
            self.cutoff_at,
            self.screening,
        ):
            raise ValueError("research selection binding does not match its frozen screening")
        if tuple(security_ids) != tuple(self.screening.selected_member_ids):
            raise ValueError("research members must preserve the frozen selected cohort order")
        if any(member.knowledge_cutoff != self.knowledge_cutoff for member in self.members):
            raise ValueError("all research members must share the frozen knowledge cutoff")
        rejected_member_ids = set(self.risk_rejected_member_ids)
        if len(rejected_member_ids) != len(self.risk_rejected_member_ids):
            raise ValueError("risk rejected member identities must be unique")
        if not rejected_member_ids.issubset(security_ids):
            raise ValueError("risk rejected members must belong to the fixed-ten cohort")
        if self.risk_scenario == "REJECT" and rejected_member_ids:
            raise ValueError("risk rejected member identities require the mixed ACCEPT scenario")
        if (
            _raw_score_month_index(self.raw_score_model.label_watermark_month)
            > self.cutoff_at.year * 12 + self.cutoff_at.month
            or self.raw_score_model.label_watermark_at > self.cutoff_at
        ):
            raise ValueError("raw-score training window must not cross research cutoff")
        mature_months = _raw_score_mature_training_months(self.cutoff_at)
        expected_training_months = (
            mature_months[-120:] if len(mature_months) >= 120 else mature_months
        )
        expected_window_kind = "ROLLING_120" if len(mature_months) >= 120 else "EXPANDING"
        if (
            self.raw_score_model.has_sufficient_training_evidence
            and not self._legacy_decoded
            and not self._historical_decoded
            and _RESEARCH_DEFINITION_VERSION_OVERRIDE.get() is None
        ):
            if not expected_training_months:
                raise ValueError("raw-score mature training window does not reach the cutoff")
            if (
                self.raw_score_model.training_months != expected_training_months
                or self.raw_score_model.training_window_kind != expected_window_kind
                or self.raw_score_model.training_window_month_count != len(expected_training_months)
                or self.raw_score_model.training_window_start_month
                != expected_training_months[0]
            ):
                raise ValueError("raw-score mature training window does not match the cutoff")
            if self.raw_score_model.training_window_end_month != expected_training_months[-1]:
                raise ValueError("raw-score mature training window does not match the cutoff")
        evidence_ids = [
            evidence.evidence_id for member in self.members for evidence in member.evidence
        ]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("research evidence identities must be globally unique")
        if (
            any(
                evidence.evidence_contract_version != RESEARCH_EVIDENCE_CONTRACT_VERSION
                for member in self.members
                for evidence in member.evidence
            )
            and not _LEGACY_RESEARCH_EVIDENCE_DECODING.get()
            and not self._legacy_decoded
        ):
            raise ValueError("current research commands require the current evidence contract")
        evidence_contract_versions = {
            evidence.evidence_contract_version
            for member in self.members
            for evidence in member.evidence
        }
        version_override = _RESEARCH_DEFINITION_VERSION_OVERRIDE.get()
        if evidence_contract_versions == {RESEARCH_EVIDENCE_CONTRACT_VERSION}:
            expected_definition_version = (
                version_override
                or (
                    RESEARCH_PRIOR_DEFINITION_VERSION
                    if self._historical_decoded
                    else RESEARCH_DEFINITION_VERSION
                )
            )
        elif evidence_contract_versions == {RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION}:
            expected_definition_version = (
                version_override or RESEARCH_LEGACY_DEFINITION_VERSION
            )
        else:
            raise ValueError("research evidence contracts must use one Definition version")
        if any(
            cohort.research_definition_version != expected_definition_version
            for cohort in self.raw_score_model.training_cohorts
        ):
            raise ValueError(
                "raw-score training cohorts must match the executing research Definition"
            )
        return self


def _legacy_structured_facts_payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(RAW_SCORE_FEATURE_IDS):
        raise ValueError("legacy research member structured signals are incomplete")
    return {
        "revenue_growth_current": value["single_quarter_revenue_acceleration"],
        "revenue_growth_prior": 0
        if value["single_quarter_revenue_acceleration"] is not None
        else None,
        "quarter_profit_improvement": value["asset_normalized_quarter_profit_improvement"],
        "average_total_assets": 1,
        "operating_cash_flow_ttm": value["operating_cash_flow_return_on_assets"],
        "working_capital_pressure_current": value["working_capital_pressure_change"],
        "working_capital_pressure_prior": 0
        if value["working_capital_pressure_change"] is not None
        else None,
        "leverage_ratio_current": value["leverage_ratio_change"],
        "leverage_ratio_prior": 0 if value["leverage_ratio_change"] is not None else None,
        "stock_return_20d": value["industry_relative_return_20d"],
        "industry_return_20d": 0
        if value["industry_relative_return_20d"] is not None
        else None,
        "downside_semivariance_60d": value["downside_semivariance_60d"],
        "max_drawdown_60d": value["max_drawdown_60d"],
        "turnover_change": value["turnover_change"],
        "institutional_net_buy_ratio": value["institutional_net_buy_ratio"],
        "institutional_listing_frequency": value["institutional_listing_frequency"],
    }


def _legacy_raw_score_model_payload(value: object) -> RawScoreModelSnapshot:
    if not isinstance(value, dict):
        raise ValueError("legacy raw-score model must be an object")
    legacy_model = LegacyRawScoreModelSnapshot.model_validate(value)
    label_watermark_at = legacy_model.label_watermark_at or _raw_score_month_end(
        legacy_model.label_watermark_month
    )
    payload = dict(legacy_model.__dict__)
    payload.update(
        label_watermark_at=label_watermark_at,
        training_cohorts=legacy_model.training_cohorts,
        training_records=legacy_model.training_records,
    )
    return RawScoreModelSnapshot.model_construct(**payload)


def decode_legacy_research_command(value: object) -> ResearchCommand:
    """Decode historical evidence only at an explicit frozen-case boundary."""
    if isinstance(value, ResearchCommand):
        return value
    if not isinstance(value, dict):
        raise ValueError("legacy research command must be an object")
    payload = deepcopy(value)
    members = payload.get("members")
    if not isinstance(members, (list, tuple)):
        raise ValueError("legacy research command members must be a list")
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("legacy research member must be an object")
        if "structured_facts" not in member:
            member["structured_facts"] = _legacy_structured_facts_payload(
                member.get("structured_signals")
            )
        evidence_items = member.get("evidence")
        if not isinstance(evidence_items, (list, tuple)):
            raise ValueError("legacy research member evidence must be a list")
        for evidence in evidence_items:
            if not isinstance(evidence, dict):
                raise ValueError("legacy research evidence must be an object")
            evidence.setdefault(
                "evidence_contract_version",
                RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
            )
    evidence_token = _LEGACY_RESEARCH_EVIDENCE_DECODING.set(True)
    command_token = _LEGACY_RESEARCH_COMMAND_DECODING.set(True)
    version_token = _RESEARCH_DEFINITION_VERSION_OVERRIDE.set(
        RESEARCH_LEGACY_DEFINITION_VERSION
    )
    try:
        payload["raw_score_model"] = _legacy_raw_score_model_payload(
            payload.get("raw_score_model")
        )
        command = ResearchCommand.model_validate(payload)
        object.__setattr__(command, "_legacy_decoded", True)
        for member in command.members:
            object.__setattr__(member, "_legacy_decoded", True)
            for evidence in member.evidence:
                object.__setattr__(evidence, "_legacy_decoded", True)
        return command
    finally:
        _RESEARCH_DEFINITION_VERSION_OVERRIDE.reset(version_token)
        _LEGACY_RESEARCH_COMMAND_DECODING.reset(command_token)
        _LEGACY_RESEARCH_EVIDENCE_DECODING.reset(evidence_token)


def decode_historical_research_command(value: object) -> ResearchCommand:
    """Decode the prior current research command at an explicit recovery boundary."""
    if not isinstance(value, dict):
        raise ValueError("historical research command must be an object")
    payload = deepcopy(value)
    version_token = _RESEARCH_DEFINITION_VERSION_OVERRIDE.set(
        RESEARCH_PRIOR_DEFINITION_VERSION
    )
    try:
        command = ResearchCommand.model_validate(payload)
        object.__setattr__(command, "_historical_decoded", True)
        return command
    finally:
        _RESEARCH_DEFINITION_VERSION_OVERRIDE.reset(version_token)


def decode_legacy_research_member_input(value: object) -> ResearchMemberInput:
    """Decode one historical Provider payload at an explicit Run boundary."""
    if not isinstance(value, dict):
        raise ValueError("legacy research member must be an object")
    payload = deepcopy(value)
    evidence_items = payload.get("evidence")
    if not isinstance(evidence_items, (list, tuple)):
        raise ValueError("legacy research member evidence must be a list")
    payload["evidence"] = [
        (
            {
                **evidence,
                "evidence_contract_version": evidence.get(
                    "evidence_contract_version",
                    RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
                ),
            }
            if isinstance(evidence, dict)
            else evidence
        )
        for evidence in evidence_items
    ]
    token = _LEGACY_RESEARCH_EVIDENCE_DECODING.set(True)
    try:
        member = ResearchMemberInput.model_validate(payload)
        object.__setattr__(member, "_legacy_decoded", True)
        for evidence in member.evidence:
            object.__setattr__(evidence, "_legacy_decoded", True)
        return member
    finally:
        _LEGACY_RESEARCH_EVIDENCE_DECODING.reset(token)


def decode_legacy_research_tool_evidence(value: object) -> ResearchToolEvidence:
    """Decode historical Tool evidence only for a historical Run checkpoint."""
    if not isinstance(value, dict):
        raise ValueError("legacy research Tool evidence must be an object")
    payload = deepcopy(value)
    payload.setdefault(
        "evidence_contract_version",
        RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
    )
    token = _LEGACY_RESEARCH_EVIDENCE_DECODING.set(True)
    try:
        evidence = ResearchToolEvidence.model_validate(payload)
        object.__setattr__(evidence, "_legacy_decoded", True)
        return evidence
    finally:
        _LEGACY_RESEARCH_EVIDENCE_DECODING.reset(token)


class ResearchDraftMember(ResearchContract):
    """The only per-stock content the research model may produce."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    thesis: str = Field(min_length=1)
    bull_case: str = Field(min_length=1)
    bear_case: str = Field(min_length=1)
    catalysts: tuple[str, ...] = Field(min_length=1)
    falsification_conditions: tuple[str, ...] = Field(min_length=1)
    unknowns: tuple[str, ...] = Field(min_length=1)
    knowledge_cutoff: AwareDatetime


class LegacyResearchDraftMember(ResearchContract):
    """The only per-stock content the research model may produce."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    thesis: str = Field(min_length=1)
    bull_case: str = Field(min_length=1)
    bear_case: str = Field(min_length=1)
    knowledge_cutoff: AwareDatetime


class LegacyResearchDraft(ResearchContract):
    """Typed research text; it deliberately has no score, probability, or action."""

    contract_version: Literal["1.0.0"]
    members: tuple[LegacyResearchDraftMember, ...] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_draft_members(self) -> LegacyResearchDraft:
        if len({member.security_id for member in self.members}) != 10:
            raise ValueError("research draft must contain ten unique securities")
        if len({member.research_id for member in self.members}) != 10:
            raise ValueError("research draft must contain ten unique research identities")
        return self


class ResearchDraft(ResearchContract):
    """Typed research text; it deliberately has no score, probability, or action."""

    contract_version: Literal["1.0.0"]
    members: tuple[ResearchDraftMember, ...] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_draft_members(self) -> ResearchDraft:
        if len({member.security_id for member in self.members}) != 10:
            raise ValueError("research draft must contain ten unique securities")
        if len({member.research_id for member in self.members}) != 10:
            raise ValueError("research draft must contain ten unique research identities")
        return self


def legacy_research_draft_json_schema() -> dict[str, object]:
    """Return the historical schema with the original persisted model names."""
    schema = LegacyResearchDraft.model_json_schema()
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        raise ValueError("legacy research draft schema is missing its member definition")
    member_schema = definitions.pop("LegacyResearchDraftMember")
    if not isinstance(member_schema, dict):
        raise ValueError("legacy research draft member schema is invalid")
    member_schema["title"] = "ResearchDraftMember"
    definitions["ResearchDraftMember"] = member_schema
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("legacy research draft schema is missing its properties")
    members_schema = properties.get("members")
    if not isinstance(members_schema, dict):
        raise ValueError("legacy research draft schema is missing its members")
    items = members_schema.get("items")
    if not isinstance(items, dict):
        raise ValueError("legacy research draft schema is missing its member reference")
    items["$ref"] = "#/$defs/ResearchDraftMember"
    schema["title"] = "ResearchDraft"
    return schema


def historical_research_draft_member_json_schema() -> dict[str, object]:
    """Return the prior current per-member schema before debate fields were added."""
    schema = deepcopy(ResearchDraftMember.model_json_schema())
    for field_name in ("catalysts", "falsification_conditions", "unknowns"):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            properties.pop(field_name, None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [
            field_name
            for field_name in required
            if field_name not in {"catalysts", "falsification_conditions", "unknowns"}
        ]
    return schema


def _historical_draft_has_debate_fields(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    members = value.get("members")
    if not isinstance(members, (list, tuple)):
        return False
    debate_fields = ("catalysts", "falsification_conditions", "unknowns")
    member_has_fields: list[bool] = []
    for member in members:
        if not isinstance(member, dict):
            return False
        member_has_fields.append(all(field in member for field in debate_fields))
    if all(member_has_fields):
        return True
    if not any(member_has_fields):
        return False
    raise ValueError("historical research draft members must use one consistent schema")


def decode_legacy_research_draft(value: object) -> ResearchDraft:
    """Decode the historical draft into the current typed draft at a compatibility boundary."""
    if not isinstance(value, dict):
        raise ValueError("legacy research draft must be an object")
    legacy_draft = LegacyResearchDraft.model_validate(value)
    return ResearchDraft(
        contract_version=legacy_draft.contract_version,
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=member.evidence_refs,
                thesis=member.thesis,
                bull_case=member.bull_case,
                bear_case=member.bear_case,
                catalysts=(LEGACY_RESEARCH_MISSING_TEXT,),
                falsification_conditions=(LEGACY_RESEARCH_MISSING_TEXT,),
                unknowns=(LEGACY_RESEARCH_MISSING_TEXT,),
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in legacy_draft.members
        ),
    )


def decode_historical_research_draft(value: object) -> ResearchDraft:
    """Decode either prior current draft shape at an explicit recovery boundary."""
    if _historical_draft_has_debate_fields(value):
        return ResearchDraft.model_validate(value)
    return decode_legacy_research_draft(value)


def decode_historical_research_draft_member(value: object) -> ResearchDraftMember:
    """Decode one prior current member output in either persisted schema shape."""
    if not isinstance(value, dict):
        raise ValueError("historical research draft member must be an object")
    debate_fields = ("catalysts", "falsification_conditions", "unknowns")
    present = [field in value for field in debate_fields]
    if all(present):
        return ResearchDraftMember.model_validate(value)
    if any(present):
        raise ValueError("historical research draft members must use one consistent schema")
    member = LegacyResearchDraftMember.model_validate(value)
    return ResearchDraftMember(
        security_id=member.security_id,
        research_id=member.research_id,
        evidence_refs=member.evidence_refs,
        thesis=member.thesis,
        bull_case=member.bull_case,
        bear_case=member.bear_case,
        catalysts=(LEGACY_RESEARCH_MISSING_TEXT,),
        falsification_conditions=(LEGACY_RESEARCH_MISSING_TEXT,),
        unknowns=(LEGACY_RESEARCH_MISSING_TEXT,),
        knowledge_cutoff=member.knowledge_cutoff,
    )


def _research_draft_has_debate_fields(draft: ResearchDraft) -> bool:
    member_has_debate_fields = tuple(
        member.catalysts != (LEGACY_RESEARCH_MISSING_TEXT,)
        and member.falsification_conditions != (LEGACY_RESEARCH_MISSING_TEXT,)
        and member.unknowns != (LEGACY_RESEARCH_MISSING_TEXT,)
        for member in draft.members
    )
    if all(member_has_debate_fields):
        return True
    if not any(member_has_debate_fields):
        return False
    raise ValueError("research draft members must use one consistent schema")


def research_draft_payload(
    draft: ResearchDraft,
    *,
    legacy: bool = False,
    historical: bool = False,
) -> dict[str, object]:
    """Serialize draft content using the exact current or historical field set."""
    if not legacy and (not historical or _research_draft_has_debate_fields(draft)):
        return draft.model_dump(mode="json")
    return LegacyResearchDraft(
        contract_version=draft.contract_version,
        members=tuple(
            LegacyResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=member.evidence_refs,
                thesis=member.thesis,
                bull_case=member.bull_case,
                bear_case=member.bear_case,
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in draft.members
        ),
    ).model_dump(mode="json")


class RiskGate(ResearchContract):
    gate_id: str = Field(min_length=1)
    status: Literal["PASSED", "FAILED"]


class RiskMemberVeto(ResearchContract):
    """Independent risk disposition and gates for one fixed cohort member."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    disposition: Literal["ACCEPTED", "REJECTED"]
    gates: tuple[RiskGate, ...] = Field(min_length=1)
    reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_disposition_against_gates(self) -> RiskMemberVeto:
        expected = "REJECTED" if any(gate.status == "FAILED" for gate in self.gates) else "ACCEPTED"
        if self.disposition != expected:
            raise ValueError("risk member disposition must match its gates")
        return self


class RiskVetoDraft(ResearchContract):
    """Typed output of the independent risk Definition and Run."""

    contract_version: Literal["1.0.0"]
    handoff_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["ACCEPTED", "REJECTED"]
    gates: tuple[RiskGate, ...] = Field(min_length=1)
    reasons: tuple[str, ...] = Field(min_length=1)
    member_vetoes: tuple[RiskMemberVeto, ...] = ()

    @model_validator(mode="after")
    def validate_member_vetoes(self) -> RiskVetoDraft:
        if self.member_vetoes:
            member_ids = [member.security_id for member in self.member_vetoes]
            research_ids = [member.research_id for member in self.member_vetoes]
            if len(member_ids) != 10 or len(set(member_ids)) != 10:
                raise ValueError("risk veto must contain one result for each fixed-ten member")
            if len(set(research_ids)) != 10:
                raise ValueError("risk veto research identities must be independent and unique")
            member_dispositions = {member.disposition for member in self.member_vetoes}
            expected = "REJECTED" if "REJECTED" in member_dispositions else "ACCEPTED"
            if self.disposition != expected:
                raise ValueError("cohort risk disposition must summarize member vetoes")
        return self


class RawScore(ResearchContract):
    """The structured, uncalibrated terminal-target score frozen after the cohort."""

    security_id: str
    research_id: str
    target: Literal["SIX_MONTH_TERMINAL_20_PERCENT"]
    algorithm: Literal["ELASTIC_NET_LOGISTIC"]
    model_version: str
    training_window_id: str
    training_window_policy: Literal["EXPANDING_60_TO_119_ROLLING_120"]
    training_window_kind: Literal["EXPANDING", "ROLLING_120"]
    training_window_month_count: int
    training_window_start_month: str
    training_window_end_month: str
    training_months: tuple[str, ...]
    label_watermark_month: str
    label_watermark_at: AwareDatetime
    normalization_snapshot_id: str
    mature_months: int
    training_record_count: int
    positive_record_count: int
    negative_record_count: int
    intercept: Decimal
    structured_inputs: dict[str, Decimal]
    transformed_inputs: dict[str, Decimal]
    coefficients: dict[str, Decimal]
    feature_transformations: dict[str, RawScoreFeatureTransform]
    contributions: dict[str, Decimal]
    z20: Decimal
    probability: None = None
    interaction_terms: tuple[str, ...] = ()
    l1_ratio: Decimal
    l2_ratio: Decimal
    penalty_strength: Decimal
    _persisted_payload: dict[str, object] | None = PrivateAttr(default=None)


def research_raw_score_payload(
    score: RawScore,
    *,
    legacy: bool = False,
) -> dict[str, object]:
    """Serialize one score using the exact current or legacy field set."""
    if score._persisted_payload is not None:
        return deepcopy(score._persisted_payload)
    payload = score.model_dump(mode="json")
    if legacy:
        payload.pop("label_watermark_at", None)
    return payload


class ResearchFrameworkOutput(ResearchContract):
    """Adapter envelope joining two durable Runs without adding a business score."""

    research_run_id: str = Field(min_length=1)
    research_run_ids: tuple[str, ...] = ()
    risk_run_id: str | None = None
    draft: ResearchDraft
    risk_veto: RiskVetoDraft | None = None
    raw_scores: tuple[RawScore, ...] | None = None
    tool_evidence_refs: tuple[str, ...] = ()
    tool_evidence: tuple[ResearchToolEvidence, ...] = ()
    member_handoffs: tuple[ResearchMemberHandoff, ...] = ()


def decode_legacy_research_framework_output(value: object) -> ResearchFrameworkOutput:
    """Decode a historical research envelope without changing its serialized identity."""
    return _decode_research_framework_output(value, decode_legacy_research_draft)


def _decode_research_framework_output(
    value: object,
    draft_decoder: Callable[[object], ResearchDraft],
) -> ResearchFrameworkOutput:
    if not isinstance(value, dict):
        raise ValueError("legacy research framework output must be an object")
    payload = deepcopy(value)
    draft = payload.get("draft")
    if isinstance(draft, dict):
        payload["draft"] = draft_decoder(draft).model_dump(mode="python")
    raw_score_payloads = payload.get("raw_scores")
    if isinstance(raw_score_payloads, (list, tuple)):
        payload["raw_scores"] = [
            _legacy_raw_score_payload(raw_score) for raw_score in raw_score_payloads
        ]
    tool_evidence = payload.get("tool_evidence")
    if isinstance(tool_evidence, (list, tuple)):
        payload["tool_evidence"] = [
            (
                {
                    **evidence,
                    "evidence_contract_version": evidence.get(
                        "evidence_contract_version",
                        RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
                    ),
                }
                if isinstance(evidence, dict)
                else evidence
            )
            for evidence in tool_evidence
        ]
    member_handoffs = payload.get("member_handoffs")
    if isinstance(member_handoffs, (list, tuple)):
        normalized_handoffs: list[object] = []
        for handoff in member_handoffs:
            if not isinstance(handoff, dict):
                normalized_handoffs.append(handoff)
                continue
            normalized_handoff = dict(handoff)
            evidence_items = normalized_handoff.get("evidence")
            if isinstance(evidence_items, (list, tuple)):
                normalized_handoff["evidence"] = [
                    (
                        {
                            **evidence,
                            "evidence_contract_version": evidence.get(
                                "evidence_contract_version",
                                RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
                            ),
                        }
                        if isinstance(evidence, dict)
                        else evidence
                    )
                    for evidence in evidence_items
                ]
            normalized_handoffs.append(normalized_handoff)
        payload["member_handoffs"] = normalized_handoffs
    token = _LEGACY_RESEARCH_EVIDENCE_DECODING.set(True)
    try:
        output = ResearchFrameworkOutput.model_validate(payload)
        bind_research_raw_score_payloads(output.raw_scores, raw_score_payloads)
        for tool_evidence in output.tool_evidence:
            object.__setattr__(tool_evidence, "_legacy_decoded", True)
        for handoff in output.member_handoffs:
            for member_evidence in handoff.evidence:
                object.__setattr__(member_evidence, "_legacy_decoded", True)
        return output
    finally:
        _LEGACY_RESEARCH_EVIDENCE_DECODING.reset(token)


def decode_historical_research_framework_output(value: object) -> ResearchFrameworkOutput:
    """Decode the prior current envelope in either persisted draft schema shape."""
    return _decode_research_framework_output(value, decode_historical_research_draft)


@dataclass(frozen=True)
class ResearchRiskPlan:
    """Pure, immutable input plan for the independent risk Definition."""

    raw_scores: tuple[RawScore, ...]
    tool_evidence_refs: tuple[str, ...]
    tool_evidence: tuple[ResearchToolEvidence, ...]
    member_handoffs: tuple[ResearchMemberHandoff, ...]
    risk_run_id: str
    handoff_fingerprint: str
    input_payload: str
    research_run_ids: tuple[str, ...] = ()


class RiskVetoOutcome(ResearchContract):
    run_id: str
    definition_id: str
    definition_version: str
    disposition: Literal["ACCEPTED", "REJECTED"]
    gates: tuple[RiskGate, ...]
    reasons: tuple[str, ...]
    member_vetoes: tuple[RiskMemberVeto, ...] = ()


class ResearchHandoff(ResearchContract):
    """Versioned, immutable boundary between research/risk and later work."""

    contract_version: Literal["1.0.0"]
    scope: Literal["D0_SYNTHETIC_RESEARCH_ONLY"]
    selection_object_id: str
    selection_event_id: str
    selection_fingerprint: str
    security_ids: tuple[str, ...] = Field(min_length=10, max_length=10)
    targets: tuple[str, ...] = Field(min_length=2, max_length=2)
    cutoff_at: AwareDatetime
    knowledge_cutoff: AwareDatetime
    evidence_ids: tuple[str, ...] = Field(min_length=10)
    research_run_id: str
    research_run_ids: tuple[str, ...] = ()
    risk_run_id: str
    research_definition_id: str
    research_definition_version: str
    risk_definition_id: str
    risk_definition_version: str
    research_model_adapter_id: str
    risk_model_adapter_id: str
    research_routing_policy_version: str
    research_output_contract_id: str
    research_output_contract_version: str
    risk_output_contract_id: str
    risk_output_contract_version: str
    screening_strategy_version: str
    screening_snapshot_id: str
    raw_scores: tuple[RawScore, ...] = Field(min_length=10, max_length=10)
    tool_evidence: tuple[ResearchToolEvidence, ...] = ()
    member_handoffs: tuple[ResearchMemberHandoff, ...] = ()
    risk_veto: RiskVetoOutcome | None = None
    actionable: Literal[False] = False

    @model_validator(mode="after")
    def validate_targets(self) -> ResearchHandoff:
        if self.targets != (
            "SIX_MONTH_POSITIVE_RETURN",
            "SIX_MONTH_TERMINAL_20_PERCENT",
        ):
            raise ValueError("research handoff must bind the two frozen screening targets")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("research handoff evidence identities must be unique")
        if self.research_run_ids:
            if len(self.research_run_ids) != len(self.security_ids):
                raise ValueError("research handoff must bind one Run to every member")
            if len(set(self.research_run_ids)) != len(self.research_run_ids):
                raise ValueError("research member Run identities must be unique")
            if self.research_run_ids[0] != self.research_run_id:
                raise ValueError("research handoff primary Run must be the first member Run")
            if self.member_handoffs and tuple(
                member.research_run_id for member in self.member_handoffs
            ) != self.research_run_ids:
                raise ValueError("research member handoffs must bind the member Run identities")
        return self


class ResearchMemberResult(ResearchContract):
    security_id: str
    research_id: str
    evidence_refs: tuple[str, ...]
    thesis: str
    bull_case: str
    bear_case: str
    catalysts: tuple[str, ...] = Field(min_length=1)
    falsification_conditions: tuple[str, ...] = Field(min_length=1)
    unknowns: tuple[str, ...] = Field(min_length=1)
    knowledge_cutoff: AwareDatetime


class ResearchOutcome(ResearchContract):
    """Host-owned research result, including independent risk disposition."""

    disposition: Literal["FROZEN", "REJECTED", "DATA_FAILED", "SYSTEM_FAILED", "BLOCKED"]
    members: tuple[ResearchMemberResult, ...]
    raw_scores: tuple[RawScore, ...] | None = None
    risk_veto: RiskVetoOutcome | None = None
    tool_evidence: tuple[ResearchToolEvidence, ...] = ()
    handoff: ResearchHandoff
    reasons: tuple[str, ...] = Field(min_length=1)
    actionable: Literal[False] = False
    _persisted_payload: dict[str, object] | None = PrivateAttr(default=None)


def decode_legacy_research_outcome(value: object) -> ResearchOutcome:
    """Decode historical outcome evidence at the frozen case boundary."""
    if not isinstance(value, dict):
        raise ValueError("legacy research outcome must be an object")
    payload = deepcopy(value)

    def normalize_raw_scores(value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        return [_legacy_raw_score_payload(raw_score) for raw_score in value]

    raw_scores = payload.get("raw_scores")
    payload["raw_scores"] = normalize_raw_scores(raw_scores)
    members = payload.get("members")
    if isinstance(members, (list, tuple)):
        normalized_members: list[object] = []
        for member in members:
            if not isinstance(member, dict):
                normalized_members.append(member)
                continue
            normalized_member = dict(member)
            normalized_member.setdefault(
                "catalysts",
                (LEGACY_RESEARCH_MISSING_TEXT,),
            )
            normalized_member.setdefault(
                "falsification_conditions",
                (LEGACY_RESEARCH_MISSING_TEXT,),
            )
            normalized_member.setdefault(
                "unknowns",
                (LEGACY_RESEARCH_MISSING_TEXT,),
            )
            normalized_members.append(normalized_member)
        payload["members"] = normalized_members
    tool_evidence = payload.get("tool_evidence")
    if isinstance(tool_evidence, (list, tuple)):
        payload["tool_evidence"] = [
            (
                {
                    **evidence,
                    "evidence_contract_version": evidence.get(
                        "evidence_contract_version",
                        RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
                    ),
                }
                if isinstance(evidence, dict)
                else evidence
            )
            for evidence in tool_evidence
        ]
    handoff = payload.get("handoff")
    handoff_raw_score_payloads = (
        deepcopy(handoff.get("raw_scores"))
        if isinstance(handoff, dict)
        else None
    )
    if isinstance(handoff, dict):
        normalized_handoff = dict(handoff)
        normalized_handoff["raw_scores"] = normalize_raw_scores(
            normalized_handoff.get("raw_scores")
        )
        member_handoffs = normalized_handoff.get("member_handoffs")
        if isinstance(member_handoffs, (list, tuple)):
            normalized_handoff_members: list[object] = []
            for member in member_handoffs:
                if not isinstance(member, dict):
                    normalized_handoff_members.append(member)
                    continue
                normalized_member = dict(member)
                evidence_items = normalized_member.get("evidence")
                if isinstance(evidence_items, (list, tuple)):
                    normalized_member["evidence"] = [
                        (
                            {
                                **evidence,
                                "evidence_contract_version": evidence.get(
                                    "evidence_contract_version",
                                    RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
                                ),
                            }
                            if isinstance(evidence, dict)
                            else evidence
                        )
                        for evidence in evidence_items
                    ]
                normalized_handoff_members.append(normalized_member)
            normalized_handoff["member_handoffs"] = normalized_handoff_members
        handoff_tool_evidence = normalized_handoff.get("tool_evidence")
        if isinstance(handoff_tool_evidence, (list, tuple)):
            normalized_handoff["tool_evidence"] = [
                (
                    {
                        **evidence,
                        "evidence_contract_version": evidence.get(
                            "evidence_contract_version",
                            RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
                        ),
                    }
                    if isinstance(evidence, dict)
                    else evidence
                )
                for evidence in handoff_tool_evidence
            ]
        payload["handoff"] = normalized_handoff
    token = _LEGACY_RESEARCH_EVIDENCE_DECODING.set(True)
    try:
        outcome = ResearchOutcome.model_validate(payload)
        object.__setattr__(outcome, "_persisted_payload", deepcopy(value))
        bind_research_raw_score_payloads(outcome.raw_scores, raw_scores)
        bind_research_raw_score_payloads(
            outcome.handoff.raw_scores,
            handoff_raw_score_payloads,
        )
        for tool_evidence in outcome.tool_evidence:
            object.__setattr__(tool_evidence, "_legacy_decoded", True)
        for handoff_tool_evidence in outcome.handoff.tool_evidence:
            object.__setattr__(handoff_tool_evidence, "_legacy_decoded", True)
        for member in outcome.handoff.member_handoffs:
            for member_evidence in member.evidence:
                object.__setattr__(member_evidence, "_legacy_decoded", True)
        return outcome
    finally:
        _LEGACY_RESEARCH_EVIDENCE_DECODING.reset(token)


def _legacy_raw_score_payload(value: object) -> object:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    if "label_watermark_at" not in normalized:
        label_watermark_month = normalized.get("label_watermark_month")
        if not isinstance(label_watermark_month, str):
            raise ValueError("legacy raw score is missing its label watermark month")
        normalized["label_watermark_at"] = _raw_score_month_end(label_watermark_month).isoformat()
    return normalized


def bind_research_raw_score_payloads(
    scores: tuple[RawScore, ...] | None,
    payloads: object,
) -> None:
    """Retain the exact historical raw-score field set beside normalized models."""
    if scores is None or not isinstance(payloads, (list, tuple)):
        return
    for score, payload in zip(scores, payloads, strict=False):
        if isinstance(payload, dict):
            object.__setattr__(score, "_persisted_payload", deepcopy(payload))


def decode_historical_research_outcome(value: object) -> ResearchOutcome:
    """Decode the prior current outcome while retaining current evidence clocks."""
    return decode_legacy_research_outcome(value)


def _legacy_evidence_payload(value: object) -> object:
    if not isinstance(value, dict):
        return value
    payload = {key: item for key, item in value.items() if item is not None}
    payload.pop("evidence_contract_version", None)
    return payload


def research_outcome_payload(
    outcome: ResearchOutcome,
    *,
    legacy: bool = False,
    historical_without_debate_fields: bool = False,
) -> dict[str, object]:
    """Serialize a research outcome without rewriting a decoded persisted shape."""
    if outcome._persisted_payload is not None:
        return deepcopy(outcome._persisted_payload)
    payload = outcome.model_dump(mode="json")
    if not (legacy or historical_without_debate_fields):
        return payload
    for member in payload.get("members", ()):
        if isinstance(member, dict):
            for field_name in ("catalysts", "falsification_conditions", "unknowns"):
                member.pop(field_name, None)
    if legacy:
        if outcome.raw_scores is not None:
            payload["raw_scores"] = [
                research_raw_score_payload(score, legacy=True)
                for score in outcome.raw_scores
            ]
        payload["tool_evidence"] = [
            _legacy_evidence_payload(evidence) for evidence in payload.get("tool_evidence", ())
        ]
        handoff = payload.get("handoff")
        if isinstance(handoff, dict):
            handoff["raw_scores"] = [
                research_raw_score_payload(score, legacy=True)
                for score in outcome.handoff.raw_scores
            ]
            handoff["tool_evidence"] = [
                _legacy_evidence_payload(evidence) for evidence in handoff.get("tool_evidence", ())
            ]
            member_handoffs = handoff.get("member_handoffs")
            if isinstance(member_handoffs, list):
                for member_handoff in member_handoffs:
                    if not isinstance(member_handoff, dict):
                        continue
                    member_handoff.pop("research_run_id", None)
                    member_handoff["evidence"] = [
                        _legacy_evidence_payload(evidence)
                        for evidence in member_handoff.get("evidence", ())
                    ]
    return payload


def research_outcome_raw_score_payloads(
    outcome: ResearchOutcome | None,
) -> tuple[dict[str, object], ...] | None:
    """Read the exact raw-score payloads bound to a persisted research outcome."""
    if outcome is None:
        return None
    payload = research_outcome_payload(outcome)
    handoff = payload.get("handoff")
    raw_scores = (
        handoff.get("raw_scores")
        if isinstance(handoff, dict)
        else payload.get("raw_scores")
    )
    if not isinstance(raw_scores, (list, tuple)) or not all(
        isinstance(score, dict) for score in raw_scores
    ):
        return None
    return tuple(deepcopy(score) for score in raw_scores)


def _transform_raw_score_feature(
    value: Decimal,
    transform: RawScoreFeatureTransform,
) -> Decimal:
    """Apply the frozen clip, robust scale, and direction before scoring."""
    clipped = min(max(value, transform.lower_clip), transform.upper_clip)
    normalized = (clipped - transform.median) / transform.iqr
    return -normalized if transform.reverse else normalized


def freeze_raw_score(command: ResearchCommand, member: ResearchMemberInput) -> RawScore:
    """Compute z20 only from structured screening priors and eleven signals."""
    if member.security_id not in command.screening.selected_member_ids:
        raise ValueError("raw score member is outside the fixed-ten cohort")
    if any(entry.completeness != "COMPLETE" for entry in member.data_manifest.entries):
        raise RawScoreCalculationError("RESEARCH_DATA_UNAVAILABLE")
    structured_signals = calculate_structured_signals(member.structured_facts)
    if any(value is None for value in structured_signals.values()):
        raise RawScoreCalculationError("RESEARCH_DATA_UNAVAILABLE")
    model = command.raw_score_model
    if not model.has_sufficient_training_evidence:
        raise RawScoreCalculationError("RAW_SCORE_MODEL_EVIDENCE_INSUFFICIENT")
    structured_inputs = {
        "screening_positive_prior": command.screening.positive_percentiles[member.security_id],
        "screening_terminal_prior": command.screening.terminal_percentiles[member.security_id],
        **{
            signal_id: value for signal_id, value in structured_signals.items() if value is not None
        },
    }
    try:
        with localcontext(_RAW_SCORE_DECIMAL_CONTEXT):
            transformed_inputs = {
                "screening_positive_prior": structured_inputs["screening_positive_prior"],
                "screening_terminal_prior": structured_inputs["screening_terminal_prior"],
                **{
                    signal_id: _transform_raw_score_feature(
                        value,
                        model.transformations[signal_id],
                    )
                    for signal_id, value in structured_signals.items()
                    if value is not None
                },
            }
            contributions = {
                key: transformed_inputs[key] * model.coefficients[key] for key in transformed_inputs
            }
            z20 = model.intercept + sum(contributions.values(), Decimal("0"))
            if not z20.is_finite() or any(
                not value.is_finite() for value in contributions.values()
            ):
                raise ArithmeticError("raw score is not finite")
    except ArithmeticError as error:
        raise RawScoreCalculationError("RAW_SCORE_CALCULATION_FAILED") from error
    return RawScore(
        security_id=member.security_id,
        research_id=member.research_id,
        target=RAW_SCORE_TARGET,
        algorithm=model.algorithm,
        model_version=model.model_version,
        training_window_id=model.training_window_id,
        training_window_policy=model.training_window_policy,
        training_window_kind=model.training_window_kind,
        training_window_month_count=model.training_window_month_count,
        training_window_start_month=model.training_window_start_month,
        training_window_end_month=model.training_window_end_month,
        training_months=model.training_months,
        label_watermark_month=model.label_watermark_month,
        label_watermark_at=model.label_watermark_at,
        normalization_snapshot_id=model.normalization_snapshot_id,
        mature_months=model.mature_months,
        training_record_count=model.training_record_count,
        positive_record_count=model.positive_record_count,
        negative_record_count=model.negative_record_count,
        intercept=model.intercept,
        structured_inputs=structured_inputs,
        transformed_inputs=transformed_inputs,
        coefficients=dict(model.coefficients),
        feature_transformations=dict(model.transformations),
        contributions=contributions,
        z20=z20,
        interaction_terms=model.interaction_terms,
        l1_ratio=model.l1_ratio,
        l2_ratio=model.l2_ratio,
        penalty_strength=model.penalty_strength,
    )


def screening_output_sha256(screening: FrozenDualTargetScreening) -> str:
    """Hash the canonical dual-head output without its self-referential digest."""
    payload = screening.model_dump(mode="json", exclude={"output_sha256"})
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def selection_binding_sha256(
    selection_object_id: str,
    selection_event_id: str,
    cutoff_at: AwareDatetime,
    screening: FrozenDualTargetScreening,
) -> str:
    """Bind the upstream selection identity to its ordered screening artifact."""
    payload = {
        "selection_object_id": selection_object_id,
        "selection_event_id": selection_event_id,
        "cutoff_at": cutoff_at.isoformat(),
        "screening": screening.model_dump(mode="json"),
    }
    return sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _research_command_fingerprint_payload(
    command: ResearchCommand,
    *,
    legacy: bool,
) -> dict[str, object]:
    payload = command.model_dump(mode="json")
    if legacy:
        for member in payload["members"]:
            for evidence in member["evidence"]:
                evidence.pop("evidence_contract_version", None)
                evidence.pop("effective_at", None)
                evidence.pop("source_published_at", None)
    return payload


def _research_tool_evidence_fingerprint_payload(
    evidence: ResearchToolEvidence,
    *,
    legacy: bool,
) -> dict[str, object]:
    payload = evidence.model_dump(mode="json")
    if legacy:
        payload.pop("evidence_contract_version", None)
        payload.pop("effective_at", None)
        payload.pop("source_published_at", None)
    return payload


def _research_member_handoff_fingerprint_payload(
    member: ResearchMemberHandoff,
    *,
    legacy: bool,
) -> dict[str, object]:
    return research_member_handoff_payload(member, legacy=legacy)


def research_member_handoff_payload(
    member: ResearchMemberHandoff,
    *,
    legacy: bool = False,
) -> dict[str, object]:
    """Serialize a member handoff using the exact current or historical field set."""
    payload = member.model_dump(mode="json")
    if legacy:
        payload.pop("research_run_id", None)
        for evidence in payload["evidence"]:
            evidence.pop("evidence_contract_version", None)
            evidence.pop("effective_at", None)
            evidence.pop("source_published_at", None)
    return payload


def handoff_fingerprint(
    command: ResearchCommand,
    draft: ResearchDraft,
    *,
    raw_scores: tuple[RawScore, ...] | None = None,
    tool_evidence_refs: tuple[str, ...] = (),
    tool_evidence: tuple[ResearchToolEvidence, ...] = (),
    member_handoffs: tuple[ResearchMemberHandoff, ...] = (),
    legacy: bool = False,
    historical: bool = False,
) -> str:
    """Hash the immutable input, typed draft, raw scores, and tool evidence."""
    payload = {
        "command": _research_command_fingerprint_payload(command, legacy=legacy),
        "draft": research_draft_payload(
            draft,
            legacy=legacy,
            historical=historical,
        ),
        "raw_scores": (
            tuple(research_raw_score_payload(score, legacy=legacy) for score in raw_scores)
            if raw_scores is not None
            else None
        ),
        "tool_evidence_refs": tool_evidence_refs,
        "tool_evidence": tuple(
            _research_tool_evidence_fingerprint_payload(evidence, legacy=legacy)
            for evidence in tool_evidence
        ),
        "member_handoffs": tuple(
            _research_member_handoff_fingerprint_payload(member, legacy=legacy)
            for member in member_handoffs
        ),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def risk_run_id_for(
    research_run_id: str,
    draft: ResearchDraft,
    *,
    raw_scores: tuple[RawScore, ...] | None = None,
    tool_evidence_refs: tuple[str, ...] = (),
    tool_evidence: tuple[ResearchToolEvidence, ...] = (),
    member_handoffs: tuple[ResearchMemberHandoff, ...] = (),
    legacy: bool = False,
    historical: bool = False,
) -> str:
    """Derive the independent risk Run identity from the frozen research Run."""
    digest = sha256(
        json.dumps(
            {
                "research_run_id": research_run_id,
                "draft": research_draft_payload(
                    draft,
                    legacy=legacy,
                    historical=historical,
                ),
                "raw_scores": (
                    tuple(research_raw_score_payload(score, legacy=legacy) for score in raw_scores)
                    if raw_scores is not None
                    else None
                ),
                "tool_evidence_refs": tool_evidence_refs,
                "tool_evidence": tuple(
                    _research_tool_evidence_fingerprint_payload(evidence, legacy=legacy)
                    for evidence in tool_evidence
                ),
                "member_handoffs": tuple(
                    _research_member_handoff_fingerprint_payload(member, legacy=legacy)
                    for member in member_handoffs
                ),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return f"risk-run-{digest}"


def research_member_results(draft: ResearchDraft) -> tuple[ResearchMemberResult, ...]:
    return tuple(
        ResearchMemberResult(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence_refs=member.evidence_refs,
            thesis=member.thesis,
            bull_case=member.bull_case,
            bear_case=member.bear_case,
            catalysts=member.catalysts,
            falsification_conditions=member.falsification_conditions,
            unknowns=member.unknowns,
            knowledge_cutoff=member.knowledge_cutoff,
        )
        for member in draft.members
    )
