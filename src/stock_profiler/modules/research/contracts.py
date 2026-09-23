"""Frozen research, raw-score, risk-veto, and handoff contracts."""

from __future__ import annotations

import json
import re
from calendar import monthrange
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from hashlib import sha256
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, model_validator

RESEARCH_CONTRACT_VERSION = "1.0.0"
RESEARCH_DEFINITION_ID = "synthetic-monthly-research"
RESEARCH_DEFINITION_VERSION = "2.0.0"
RESEARCH_MODEL_ADAPTER_ID = "m-agent-deterministic-research-adapter"
RESEARCH_ROUTING_POLICY_VERSION = "monthly-research-risk-veto-v1"
RESEARCH_OUTPUT_CONTRACT_ID = "synthetic-monthly-research-draft"
RESEARCH_OUTPUT_CONTRACT_VERSION = "1.0.0"
RISK_DEFINITION_ID = "synthetic-independent-risk-veto"
RISK_DEFINITION_VERSION = "1.0.0"
RISK_MODEL_ADAPTER_ID = "m-agent-deterministic-risk-adapter"
RISK_OUTPUT_CONTRACT_ID = "synthetic-independent-risk-veto"
RISK_OUTPUT_CONTRACT_VERSION = "1.0.0"
RESEARCH_SCOPE: Literal["D0_SYNTHETIC_RESEARCH_ONLY"] = "D0_SYNTHETIC_RESEARCH_ONLY"
RESEARCH_ANNOUNCEMENT_TOOL_VERSION = "synthetic-announcement-tool-v1"
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

    model_config = ConfigDict(extra="forbid", frozen=True)


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
            or self.research_definition_version != RESEARCH_DEFINITION_VERSION
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
        if self.label_watermark_at.strftime("%Y-%m") != self.label_watermark_month:
            raise ValueError("raw-score label watermark date does not match its month")
        security_month_keys = [
            (record.month, record.security_id) for record in self.training_records
        ]
        if len(set(security_month_keys)) != len(security_month_keys):
            raise ValueError("raw-score training records must have unique security-month evidence")
        cohorts_by_id = {cohort.cohort_id: cohort for cohort in self.training_cohorts}
        if len(cohorts_by_id) != len(self.training_cohorts):
            raise ValueError("raw-score training cohorts must have unique identities")
        if {cohort.month for cohort in self.training_cohorts} != set(self.training_months):
            raise ValueError("raw-score training cohorts must cover every training month")
        if len({cohort.month for cohort in self.training_cohorts}) != len(self.training_cohorts):
            raise ValueError("raw-score training cohorts must have one cohort per month")
        records_by_cohort: dict[str, list[RawScoreTrainingRecord]] = {}
        for record in self.training_records:
            cohort = cohorts_by_id.get(record.cohort_id)
            if cohort is None:
                raise ValueError("raw-score training record must bind to a frozen cohort")
            if record.month != cohort.month:
                raise ValueError("raw-score training record month must match its frozen cohort")
            if record.security_id not in cohort.completed_research_ids:
                raise ValueError("raw-score training record must bind to a frozen cohort member")
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
        if self.positive_record_count + self.negative_record_count != self.training_record_count:
            raise ValueError("raw-score training records must equal the two class counts")
        positive_records = sum(record.terminal_label for record in self.training_records)
        if self.positive_record_count != positive_records:
            raise ValueError("raw-score positive record count does not match records")
        if self.negative_record_count != self.training_record_count - positive_records:
            raise ValueError("raw-score negative record count does not match records")
        if any(
            record.source_model_version != self.model_version for record in self.training_records
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
            _raw_score_label_available_at(record.evaluation_entry_at) > record.label_available_at
            for record in self.training_records
        ):
            raise ValueError("raw-score training record label maturity is incomplete")
        if any(
            record.label_available_at > self.label_watermark_at for record in self.training_records
        ):
            raise ValueError("raw-score training record exceeds the label availability watermark")
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
    training_months = _training_month_sequence(2036, 12, 60)
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
        training_window_start_month="2036-12",
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
    effective_at: AwareDatetime
    source_published_at: AwareDatetime
    acquired_at: AwareDatetime
    validated_at: AwareDatetime
    knowledge_cutoff: AwareDatetime
    semantic_version: str = Field(min_length=1)
    validation_status: Literal["VALIDATED"]

    @model_validator(mode="after")
    def validate_clocks(self) -> ResearchEvidence:
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
    effective_at: AwareDatetime
    source_published_at: AwareDatetime
    acquired_at: AwareDatetime
    validated_at: AwareDatetime
    knowledge_cutoff: AwareDatetime
    semantic_version: str = Field(min_length=1)
    validation_status: Literal["VALIDATED"]

    @model_validator(mode="after")
    def validate_clocks(self) -> ResearchToolEvidence:
        _validate_evidence_clocks(
            source_published_at=self.source_published_at,
            acquired_at=self.acquired_at,
            validated_at=self.validated_at,
            knowledge_cutoff=self.knowledge_cutoff,
        )
        return self


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

    @model_validator(mode="after")
    def validate_stage_progression(self) -> ResearchStageArtifact:
        expected_source = {
            "analyze": "collect",
            "bull-bear": "analyze",
            "draft": "bull-bear",
        }[self.stage_id]
        if self.source_stage_id != expected_source:
            raise ValueError("research stage artifact source does not match its stage")
        if len(set(self.input_item_ids)) != len(self.input_item_ids):
            raise ValueError("research stage artifact input identities must be unique")
        if self.stage_id in {"bull-bear", "draft"} and (not self.bull_case or not self.bear_case):
            raise ValueError("research debate artifacts require both bull and bear cases")
        return self


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


def calculate_structured_signals(
    facts: ResearchStructuredFacts,
) -> dict[str, Decimal | None]:
    """Calculate the frozen raw-score signals from named source facts."""
    with localcontext(_RAW_SCORE_DECIMAL_CONTEXT):
        return {
            "single_quarter_revenue_acceleration": _structured_difference(
                facts.revenue_growth_current,
                facts.revenue_growth_prior,
            ),
            "asset_normalized_quarter_profit_improvement": _structured_ratio(
                facts.quarter_profit_improvement,
                facts.average_total_assets,
            ),
            "operating_cash_flow_return_on_assets": _structured_ratio(
                facts.operating_cash_flow_ttm,
                facts.average_total_assets,
            ),
            "working_capital_pressure_change": _structured_ratio(
                _structured_difference(
                    facts.working_capital_pressure_current,
                    facts.working_capital_pressure_prior,
                ),
                facts.average_total_assets,
            ),
            "leverage_ratio_change": _structured_difference(
                facts.leverage_ratio_current,
                facts.leverage_ratio_prior,
            ),
            "industry_relative_return_20d": _structured_difference(
                facts.stock_return_20d,
                facts.industry_return_20d,
            ),
            "downside_semivariance_60d": facts.downside_semivariance_60d,
            "max_drawdown_60d": facts.max_drawdown_60d,
            "turnover_change": facts.turnover_change,
            "institutional_net_buy_ratio": facts.institutional_net_buy_ratio,
            "institutional_listing_frequency": facts.institutional_listing_frequency,
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
            "single_quarter_revenue_acceleration": safe(
                lambda: _structured_difference(
                    facts.revenue_growth_current,
                    facts.revenue_growth_prior,
                )
            ),
            "asset_normalized_quarter_profit_improvement": safe(
                lambda: _structured_ratio(
                    facts.quarter_profit_improvement,
                    facts.average_total_assets,
                )
            ),
            "operating_cash_flow_return_on_assets": safe(
                lambda: _structured_ratio(
                    facts.operating_cash_flow_ttm,
                    facts.average_total_assets,
                )
            ),
            "working_capital_pressure_change": safe(
                lambda: _structured_ratio(
                    _structured_difference(
                        facts.working_capital_pressure_current,
                        facts.working_capital_pressure_prior,
                    ),
                    facts.average_total_assets,
                )
            ),
            "leverage_ratio_change": safe(
                lambda: _structured_difference(
                    facts.leverage_ratio_current,
                    facts.leverage_ratio_prior,
                )
            ),
            "industry_relative_return_20d": safe(
                lambda: _structured_difference(
                    facts.stock_return_20d,
                    facts.industry_return_20d,
                )
            ),
            "downside_semivariance_60d": facts.downside_semivariance_60d,
            "max_drawdown_60d": facts.max_drawdown_60d,
            "turnover_change": facts.turnover_change,
            "institutional_net_buy_ratio": facts.institutional_net_buy_ratio,
            "institutional_listing_frequency": facts.institutional_listing_frequency,
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
            evidence.acquired_at > evidence.validated_at
            or evidence.validated_at > self.knowledge_cutoff
            or evidence.validation_status != "VALIDATED"
            for evidence in self.evidence
        ):
            raise ValueError("research evidence must be validated and available by the cutoff")
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
        evidence_ids = [
            evidence.evidence_id for member in self.members for evidence in member.evidence
        ]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("research evidence identities must be globally unique")
        return self


class ResearchDraftMember(ResearchContract):
    """The only per-stock content the research model may produce."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    thesis: str = Field(min_length=1)
    bull_case: str = Field(min_length=1)
    bear_case: str = Field(min_length=1)
    knowledge_cutoff: AwareDatetime


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


def handoff_fingerprint(
    command: ResearchCommand,
    draft: ResearchDraft,
    *,
    raw_scores: tuple[RawScore, ...] | None = None,
    tool_evidence_refs: tuple[str, ...] = (),
    tool_evidence: tuple[ResearchToolEvidence, ...] = (),
    member_handoffs: tuple[ResearchMemberHandoff, ...] = (),
) -> str:
    """Hash the immutable input, typed draft, raw scores, and tool evidence."""
    payload = {
        "command": command.model_dump(mode="json"),
        "draft": draft.model_dump(mode="json"),
        "raw_scores": (
            tuple(score.model_dump(mode="json") for score in raw_scores)
            if raw_scores is not None
            else None
        ),
        "tool_evidence_refs": tool_evidence_refs,
        "tool_evidence": tuple(evidence.model_dump(mode="json") for evidence in tool_evidence),
        "member_handoffs": tuple(member.model_dump(mode="json") for member in member_handoffs),
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
) -> str:
    """Derive the independent risk Run identity from the frozen research Run."""
    digest = sha256(
        json.dumps(
            {
                "research_run_id": research_run_id,
                "draft": draft.model_dump(mode="json"),
                "raw_scores": (
                    tuple(score.model_dump(mode="json") for score in raw_scores)
                    if raw_scores is not None
                    else None
                ),
                "tool_evidence_refs": tool_evidence_refs,
                "tool_evidence": tuple(
                    evidence.model_dump(mode="json") for evidence in tool_evidence
                ),
                "member_handoffs": tuple(
                    member.model_dump(mode="json") for member in member_handoffs
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
            knowledge_cutoff=member.knowledge_cutoff,
        )
        for member in draft.members
    )
