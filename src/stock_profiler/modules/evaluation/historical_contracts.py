"""Parameterized preregistration and saved non-actionable historical evidence."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_profiler.foundation.decision_versions import DecisionCaseVersionBundle
from stock_profiler.modules.candidate_selection.selection import SelectionPolicy
from stock_profiler.modules.evaluation.contracts import (
    EvaluationContract,
    OutcomeEvidence,
    StandardObservation,
    TerminalEvidence,
)


class HistoricalRegistration(EvaluationContract):
    version_id: str = Field(min_length=1)
    start_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    end_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    source_version_bundle: DecisionCaseVersionBundle
    strategy_version: str = Field(min_length=1)
    market_calendar_version: str = Field(min_length=1)
    standard_quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    selection_policy: SelectionPolicy
    positive_members_required: int = Field(gt=0, strict=True)
    target_members_required: int = Field(gt=0, strict=True)
    terminal_target: Decimal = Field(gt=0, allow_inf_nan=False)
    maximum_drawdown: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    exploratory_months: int = Field(gt=0, strict=True)
    formal_months: int = Field(gt=0, strict=True)
    overall_pass_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    overall_drawdown_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    regime_pass_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    regime_drawdown_floor: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    regime_months: int = Field(gt=0, strict=True)
    regime_drawdown_months: int = Field(gt=0, strict=True)
    regime_nonoverlapping_windows: int = Field(gt=0, strict=True)
    regime_periods: int = Field(gt=0, strict=True)
    block_lengths: tuple[int, ...] = Field(min_length=1)
    confidence: Decimal = Field(gt=0, lt=1, allow_inf_nan=False)
    bootstrap_repetitions: int = Field(ge=99, strict=True)
    random_trials: int = Field(gt=0, strict=True)
    minimum_availability: Decimal = Field(gt=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_registration(self) -> "HistoricalRegistration":
        if self.end_month < self.start_month or self.formal_months <= self.exploratory_months:
            raise ValueError("historical window and maturity floors are inconsistent")
        if (
            max(self.positive_members_required, self.target_members_required)
            > self.selection_policy.cohort_size
        ):
            raise ValueError("cohort success counts exceed registered size")
        if any(length < 6 for length in self.block_lengths) or len(set(self.block_lengths)) != len(
            self.block_lengths
        ):
            raise ValueError("block lengths must be distinct and cover the six-month horizon")
        return self


class IndexPrice(EvaluationContract):
    closed_at: AwareDatetime
    total_return_price: Decimal = Field(gt=0, allow_inf_nan=False)


class SelectionIndexEvidence(OutcomeEvidence):
    prices: tuple[IndexPrice, ...]


class FutureIndexEvidence(OutcomeEvidence):
    starting_at: AwareDatetime
    starting_total_return_price: Decimal = Field(gt=0, allow_inf_nan=False)
    terminal_total_return_price: Decimal = Field(gt=0, allow_inf_nan=False)


class FactorRow(EvaluationContract):
    security_id: str
    net_income_ttm: Decimal | None = Field(allow_inf_nan=True)
    total_capitalization: Decimal | None = Field(allow_inf_nan=True)
    average_equity: Decimal | None = Field(allow_inf_nan=True)
    momentum_start_price: Decimal | None = Field(allow_inf_nan=True)
    momentum_end_price: Decimal | None = Field(allow_inf_nan=True)
    momentum_start_at: AwareDatetime | None
    momentum_end_at: AwareDatetime | None
    daily_returns: tuple[Annotated[Decimal, Field(allow_inf_nan=True)], ...]
    return_dates: tuple[AwareDatetime, ...]


class FactorEvidence(OutcomeEvidence):
    rows: tuple[FactorRow, ...]


class WealthPath(EvaluationContract):
    security_id: str
    marks: tuple[TerminalEvidence, ...]


class HistoricalMonthInput(EvaluationContract):
    plan_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    selection_event_id: str
    standard_event_id: str | None
    observations: tuple[StandardObservation, ...]
    paths: tuple[WealthPath, ...]
    index: SelectionIndexEvidence | None
    factors: FactorEvidence | None
    future_index: FutureIndexEvidence | None


class HistoricalSelectionCommand(EvaluationContract):
    operation: Literal["REGISTER", "EVALUATE"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    cutoff_at: AwareDatetime
    registration: HistoricalRegistration | None
    registration_event_id: str | None
    months: tuple[HistoricalMonthInput, ...]
    previous_event_id: str | None

    @model_validator(mode="after")
    def validate_operation(self) -> "HistoricalSelectionCommand":
        if self.operation == "REGISTER":
            if (
                self.registration is None
                or self.registration_event_id is not None
                or self.months
                or self.previous_event_id
            ):
                raise ValueError("registration must precede evaluation inputs")
        elif self.registration is not None or not self.registration_event_id:
            raise ValueError("evaluation requires one saved preregistration")
        return self


class BaselineCounts(EvaluationContract):
    """Exact success counts; failed constrained trials retain their member slots."""

    positive: int = Field(ge=0, strict=True)
    target: int = Field(ge=0, strict=True)
    member_slots: int = Field(ge=0, strict=True)
    passed_trials: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_counts(self) -> "BaselineCounts":
        if self.target > self.positive or self.positive > self.member_slots:
            raise ValueError("baseline successes exceed their exact denominator")
        return self


class BaselineResult(EvaluationContract):
    positive_rate: Decimal | None
    target_rate: Decimal | None
    batch_pass_rate: Decimal | None
    drawdown_pass_rate: Decimal | None
    trial_count: int
    failed_trials: int
    membership_digest: str
    members: tuple[str, ...] = ()
    counts: BaselineCounts | None = None


class HistoricalMonthResult(EvaluationContract):
    plan_month: str
    disposition: str
    selection_event_id: str | None = None
    selection_at: AwareDatetime | None = None
    matures_at: AwareDatetime | None = None
    members: tuple[str, ...] = ()
    regime: Literal["BULL", "BEAR", "SIDEWAYS"] | None = None
    baselines: dict[str, BaselineResult] = {}
    availability_failure: Literal["DATA", "SYSTEM"] | None = None
    positive_count: int | None = None
    target_count: int | None = None
    positive_rate: Decimal | None = None
    target_rate: Decimal | None = None
    maximum_drawdown: Decimal | None = None
    nav: tuple[Decimal, ...] = ()
    member_returns: dict[str, Decimal] = {}
    future_index_return: Decimal | None = None
    batch_pass: bool | None = None
    drawdown_pass: bool | None = None
    reasons: tuple[str, ...] = ()


class HistoricalCounts(EvaluationContract):
    planned: int
    missing_months: int
    mature_valid: int = 0
    passed_batches: int = 0
    drawdown_evaluable: int = 0
    availability_failures: int = 0
    due_missing: int = 0
    immature: int = 0


class HistoricalGate(EvaluationContract):
    estimate: Decimal | None
    lower_bound: Decimal | None
    threshold: Decimal
    strict: bool
    passed: bool | None
    block_bounds: dict[str, Decimal | None]
    undefined_resamples: dict[str, int]


class RegimeWatermark(EvaluationContract):
    mature_months: int
    formed_months: int
    nonoverlapping_windows: int
    periods: int
    sufficient: bool


class HistoricalInference(EvaluationContract):
    disposition: Literal["INSUFFICIENT", "EXPLORATORY", "PASSED", "FAILED", "INDETERMINATE"]
    gates: dict[str, HistoricalGate]
    regimes: dict[str, RegimeWatermark]
    diagnostics: dict[str, int | str | None]
    resampling: dict[str, dict[str, int | str]]


class HistoricalSelectionReport(EvaluationContract):
    disposition: Literal[
        "REGISTERED", "INSUFFICIENT", "EXPLORATORY", "PASSED", "FAILED", "INDETERMINATE"
    ]
    registration: HistoricalRegistration
    registration_event_id: str | None
    previous_event_id: str | None = None
    previous_report_id: str | None = None
    report_version: int = 1
    inference: HistoricalInference | None = None
    counts: HistoricalCounts | None = None
    months: tuple[HistoricalMonthResult, ...] = ()
    cutoff_at: AwareDatetime
    actionable: Literal[False] = False
    authorization_granted: Literal[False] = False
    qualification_scope: Literal["D0_SYNTHETIC_CONTRACT_ONLY"] = "D0_SYNTHETIC_CONTRACT_ONLY"
