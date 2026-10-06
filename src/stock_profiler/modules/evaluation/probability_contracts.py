"""Explicit synthetic preregistration for non-actionable probability evidence."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_profiler.foundation.decision_versions import DecisionCaseVersionBundle
from stock_profiler.modules.candidate_selection.calibrated_candidates import CalibrationDiagnostics
from stock_profiler.modules.evaluation.contracts import EvaluationContract
from stock_profiler.modules.evaluation.historical_contracts import SelectionIndexEvidence


class ProbabilityRegistration(EvaluationContract):
    version_id: str = Field(min_length=1)
    start_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    end_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    source_version_bundle: DecisionCaseVersionBundle
    capability_version: str = Field(min_length=1)
    raw_score_model_version: str = Field(min_length=1)
    selection_strategy_version: str = Field(min_length=1)
    minimum_availability: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    market_calendar_version: str = Field(min_length=1)
    calibrator_version: Literal["monotone-firth-logistic-v1"]
    high_band_threshold: Decimal = Field(gt=0, lt=1, allow_inf_nan=False)
    success_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    overconfidence_ceiling: Decimal = Field(ge=0, lt=1, allow_inf_nan=False)
    coverage_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    overall_months: int = Field(gt=0, strict=True)
    overall_high_band_records: int = Field(gt=0, strict=True)
    overall_nonoverlapping_windows: int = Field(gt=0, strict=True)
    regime_months: int = Field(gt=0, strict=True)
    regime_high_band_records: int = Field(gt=0, strict=True)
    regime_nonoverlapping_windows: int = Field(gt=0, strict=True)
    regime_periods: int = Field(gt=0, strict=True)
    block_lengths: tuple[int, ...]
    confidence: Decimal = Field(gt=0, lt=1, allow_inf_nan=False)
    bootstrap_repetitions: int = Field(ge=99, strict=True)

    @model_validator(mode="after")
    def validate_window(self) -> "ProbabilityRegistration":
        if self.end_month < self.start_month or self.block_lengths != (6, 9, 12):
            raise ValueError(
                "probability history requires ordered months and 6/9/12 calendar blocks"
            )
        if (
            self.high_band_threshold != Decimal(".80")
            or self.success_floor < Decimal(".80")
            or self.overconfidence_ceiling > Decimal(".05")
            or self.coverage_floor < Decimal(".50")
            or self.minimum_availability < Decimal(".95")
            or self.overall_months < 120
            or self.overall_high_band_records < 500
            or self.overall_nonoverlapping_windows < 20
            or self.regime_months < 30
            or self.regime_high_band_records < 100
            or self.regime_nonoverlapping_windows < 5
            or self.regime_periods < 2
            or self.confidence < Decimal(".95")
        ):
            raise ValueError("preregistration cannot weaken fixed probability qualification gates")
        return self


class ProbabilityIndex(EvaluationContract):
    source_event_id: str = Field(min_length=1)
    index: SelectionIndexEvidence


class HistoricalProbabilityCommand(EvaluationContract):
    operation: Literal["REGISTER", "EVALUATE"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    cutoff_at: AwareDatetime
    registration: ProbabilityRegistration | None
    registration_event_id: str | None
    previous_event_id: str | None
    indices: tuple[ProbabilityIndex, ...]

    @model_validator(mode="after")
    def validate_operation(self) -> "HistoricalProbabilityCommand":
        if self.operation == "REGISTER":
            if (
                self.registration is None
                or self.registration_event_id
                or self.previous_event_id
                or self.indices
            ):
                raise ValueError("registration must precede observed evidence")
        elif self.registration is not None or not self.registration_event_id:
            raise ValueError("evaluation requires a saved preregistration")
        if len({item.source_event_id for item in self.indices}) != len(self.indices):
            raise ValueError("index source identities must be unique")
        return self


class ProbabilityMember(EvaluationContract):
    evaluation_id: str
    source_event_id: str
    security_id: str
    raw_success_score: Decimal
    frozen_probability: Decimal
    matures_at: AwareDatetime
    state: Literal["PENDING", "UNAVAILABLE", "ACHIEVED", "NOT_ACHIEVED"]
    entry_expired: bool


class ProbabilityMonth(EvaluationContract):
    plan_month: str
    disposition: str
    source_event_id: str | None = None
    standard_event_id: str | None = None
    cutoff_at: AwareDatetime | None = None
    matures_at: AwareDatetime | None = None
    regime: Literal["BULL", "BEAR", "SIDEWAYS"] | None = None
    label_mature: bool = False
    valid_monthly: bool = False
    has_candidates: bool = False
    availability_failure: str | None = None
    members: tuple[ProbabilityMember, ...] = ()
    index: SelectionIndexEvidence | None = None
    reasons: tuple[str, ...] = ()


class ProbabilityCounts(EvaluationContract):
    planned: int
    missing_months: int
    valid_months: int
    recommendation_months: int
    availability_failures: int
    registered: int
    due: int
    evaluable: int
    due_missing: int
    immature: int
    high_band: int


class ProbabilityBound(EvaluationContract):
    estimate: Decimal | None
    bound: Decimal | None
    direction: Literal["LOWER", "UPPER"]
    threshold: Decimal
    passed: bool | None
    block_bounds: dict[str, Decimal | None]
    undefined_resamples: dict[str, int]


class ProbabilityWatermark(EvaluationContract):
    mature_months: int
    high_band_records: int
    nonoverlapping_windows: int
    periods: int
    sufficient: bool
    disposition: Literal["INSUFFICIENT", "INDETERMINATE", "PASSED", "FAILED"]


class ProbabilityInference(EvaluationContract):
    overall: ProbabilityWatermark
    regimes: dict[str, ProbabilityWatermark]
    gates: dict[str, ProbabilityBound]
    resampling: dict[str, dict[str, str | int]]
    diagnostics: CalibrationDiagnostics | None
    recent_diagnostic_months: tuple[str, ...]
    recent_diagnostic_sample_count: int
    recent_diagnostics: CalibrationDiagnostics | None


class HistoricalProbabilityReport(EvaluationContract):
    disposition: str
    registration: ProbabilityRegistration
    registration_event_id: str | None
    cutoff_at: AwareDatetime
    previous_event_id: str | None = None
    previous_report_id: str | None = None
    report_version: int = 1
    months: tuple[ProbabilityMonth, ...] = ()
    counts: ProbabilityCounts | None = None
    inference: ProbabilityInference | None = None
    actionable: Literal[False] = False
    authorization_granted: Literal[False] = False
