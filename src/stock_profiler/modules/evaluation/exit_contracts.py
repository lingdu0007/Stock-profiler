"""Frozen preregistration and independent standard exit report contracts."""

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from stock_profiler.modules.candidate_selection.calibrated_candidates import CalibrationDiagnostics
from stock_profiler.modules.evaluation.contracts import (
    EvaluationContract,
    OutcomeEvidence,
    StandardExitPosition,
)
from stock_profiler.modules.position_management.exit_contracts import ExitThesis, SecurityEvidence


class ExitCostCurve(EvaluationContract):
    commission_rate: Decimal = Field(ge=0, lt=1, allow_inf_nan=False)
    minimum_commission: Decimal = Field(ge=0, allow_inf_nan=False)
    fee_rate: Decimal = Field(ge=0, lt=1, allow_inf_nan=False)
    tax_rate: Decimal = Field(ge=0, lt=1, allow_inf_nan=False)
    slippage_rate: Decimal = Field(ge=0, lt=1, allow_inf_nan=False)

    def charge(self, gross: Decimal) -> Decimal:
        if gross <= 0:
            return Decimal(0)
        return max(self.minimum_commission, gross * self.commission_rate) + gross * (
            self.fee_rate + self.tax_rate + self.slippage_rate
        )


def canonical_loss_grid(value: Decimal | str) -> str:
    try:
        numeric = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("EXIT_LOSS_GRID_INVALID") from error
    if numeric not in tuple(map(Decimal, (".05", ".10", ".15", ".20"))):
        raise ValueError("EXIT_LOSS_GRID_INVALID")
    return format(numeric, ".2f")


def canonical_target(name: str) -> str:
    if name == "V60":
        return name
    if not name.startswith("D20:"):
        raise ValueError("EXIT_TARGET_INVALID")
    return "D20:" + canonical_loss_grid(name.split(":", 1)[1])


class ExitPolicy(EvaluationContract):
    version_id: str = Field(min_length=1)
    start_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    end_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    market_calendar_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    action_version: str = Field(min_length=1)
    cost_version: str = Field(min_length=1)
    cost_curve: ExitCostCurve
    baseline_version: str = Field(min_length=1)
    loss_grids: tuple[Decimal, ...]
    selected_grid: Decimal
    states: tuple[Literal["BULL", "BEAR", "SIDEWAYS"], ...]
    bootstrap_repetitions: int = Field(ge=99, le=10000, strict=True)
    confidence: Decimal = Field(ge=Decimal(".95"), lt=1, allow_inf_nan=False)
    block_lengths: tuple[int, ...]
    continuous_quantile: Decimal = Field(default=Decimal(".5"), gt=0, lt=1, allow_inf_nan=False)
    fixed_reduce_loss: Decimal
    fixed_exit_loss: Decimal
    trailing_reduce_loss: Decimal
    trailing_exit_loss: Decimal
    safe_downside: Decimal
    risky_downside: Decimal
    established_value: Decimal
    denied_value: Decimal

    @field_validator("selected_grid")
    @classmethod
    def canonical_selected_grid(cls, value: Decimal) -> Decimal:
        return Decimal(canonical_loss_grid(value))

    @field_validator("loss_grids")
    @classmethod
    def canonical_grids(cls, values: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
        return tuple(Decimal(canonical_loss_grid(value)) for value in values)

    @model_validator(mode="after")
    def validate_policy(self) -> "ExitPolicy":
        if self.end_month < self.start_month or self.block_lengths != (6, 9, 12):
            raise ValueError("exit history requires ordered months and common 6/9/12 blocks")
        if self.loss_grids != tuple(map(Decimal, (".05", ".10", ".15", ".20"))):
            raise ValueError("exit targets require the complete registered loss grid")
        if (
            self.selected_grid not in self.loss_grids
            or not self.states
            or len(set(self.states)) != len(self.states)
        ):
            raise ValueError("exit policy requires unique states and a registered grid")
        if (
            self.fixed_reduce_loss,
            self.fixed_exit_loss,
            self.trailing_reduce_loss,
            self.trailing_exit_loss,
        ) != tuple(map(Decimal, (".10", ".20", ".10", ".20"))):
            raise ValueError("transparent baseline rules cannot change within this contract")
        if (
            self.safe_downside,
            self.risky_downside,
            self.established_value,
            self.denied_value,
        ) != tuple(map(Decimal, (".20", ".40", ".70", ".30"))):
            raise ValueError("exit action thresholds require a new contract to change")
        return self


class ExitSession(EvaluationContract):
    closed_at: AwareDatetime
    price: Decimal = Field(ge=0, allow_inf_nan=False)
    vwap: Decimal = Field(ge=0, allow_inf_nan=False)
    sellable: bool
    blocked_reason: Literal["SUSPENDED", "LIMIT_DOWN", "NO_BIDS", "RESTRICTED"] | None
    quantity_multiplier: Decimal = Field(gt=0, allow_inf_nan=False)
    cash_per_initial_share: Decimal = Field(ge=0, allow_inf_nan=False)
    costs: ExitCostCurve

    @model_validator(mode="after")
    def validate_execution(self) -> "ExitSession":
        if self.sellable == (self.blocked_reason is not None):
            raise ValueError("standard exit session requires an explicit execution state")
        return self


class GuardedExitProbability(EvaluationContract):
    point: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    lower_error: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    upper_error: Decimal = Field(ge=0, le=1, allow_inf_nan=False)

    @property
    def lower(self) -> Decimal:
        return max(Decimal(0), self.point - self.lower_error)

    @property
    def upper(self) -> Decimal:
        return min(Decimal(1), self.point + self.upper_error)


class ExitHardFact(OutcomeEvidence):
    reason: Literal[
        "AUTHORITATIVE_DELISTING", "AUTHORITATIVE_TRADING_TERMINATION", "SECURITY_INELIGIBLE"
    ]


class ExitThesisFalsification(EvaluationContract):
    reason: Literal["THESIS_FALSIFIED"]
    thesis_event_id: str = Field(min_length=1)
    evidence: SecurityEvidence

    @property
    def evidence_id(self) -> str:
        return self.evidence.evidence_id

    @property
    def available_at(self) -> AwareDatetime:
        return max(self.evidence.public_at, self.evidence.acquired_at, self.evidence.validated_at)

    @property
    def effective_at(self) -> AwareDatetime:
        return self.evidence.public_at


class ExitForecast(EvaluationContract):
    prediction_id: str = Field(min_length=1)
    cutoff_at: AwareDatetime
    frozen_at: AwareDatetime
    training_watermark_at: AwareDatetime
    model_version: str = Field(min_length=1)
    state: Literal["BULL", "BEAR", "SIDEWAYS"]
    data_complete: bool
    qualified_scope: bool
    new_evidence_id: str = Field(min_length=1)
    downside: dict[str, GuardedExitProbability]
    value: GuardedExitProbability | None
    baseline_probabilities: dict[str, dict[str, Decimal]] = Field(default_factory=dict)
    quantiles: dict[str, Decimal] = Field(default_factory=dict)
    hard_exit: bool
    hard_evidence_id: str | None
    hard_evidence: ExitHardFact | ExitThesisFalsification | None = Field(
        default=None, discriminator="reason"
    )

    @field_validator("downside")
    @classmethod
    def canonical_downside(
        cls, values: dict[str, GuardedExitProbability]
    ) -> dict[str, GuardedExitProbability]:
        result = {canonical_loss_grid(key): value for key, value in values.items()}
        if len(result) != len(values):
            raise ValueError("EXIT_DUPLICATE_LOSS_GRID")
        return result

    @field_validator("baseline_probabilities")
    @classmethod
    def canonical_baselines(
        cls, values: dict[str, dict[str, Decimal]]
    ) -> dict[str, dict[str, Decimal]]:
        result = {
            name: {canonical_target(key): value for key, value in targets.items()}
            for name, targets in values.items()
        }
        if any(len(result[name]) != len(targets) for name, targets in values.items()):
            raise ValueError("EXIT_DUPLICATE_TARGET")
        return result

    @field_validator("quantiles")
    @classmethod
    def canonical_quantiles(cls, values: dict[str, Decimal]) -> dict[str, Decimal]:
        result = {canonical_target(key): value for key, value in values.items()}
        if len(result) != len(values):
            raise ValueError("EXIT_DUPLICATE_TARGET")
        return result

    @model_validator(mode="after")
    def validate_freeze(self) -> "ExitForecast":
        if self.frozen_at != self.cutoff_at or self.training_watermark_at >= self.cutoff_at:
            raise ValueError("EXIT_FORECAST_NOT_FROZEN_OUT_OF_SAMPLE")
        if self.hard_exit != bool(self.hard_evidence_id):
            raise ValueError("deterministic exit requires authoritative fact identity")
        evidence = self.hard_evidence
        if self.hard_exit and (
            evidence is None
            or evidence.evidence_id != self.hard_evidence_id
            or evidence.available_at > self.cutoff_at
            or evidence.effective_at > self.cutoff_at
        ):
            raise ValueError("EXIT_HARD_EVIDENCE_UNAVAILABLE")
        if not self.hard_exit and evidence is not None:
            raise ValueError("EXIT_HARD_EVIDENCE_WITHOUT_GATE")
        if any(
            not value.is_finite() or not 0 <= value <= 1
            for probabilities in self.baseline_probabilities.values()
            for value in probabilities.values()
        ) or any(not value.is_finite() for value in self.quantiles.values()):
            raise ValueError("EXIT_CALIBRATION_EVIDENCE_INVALID")
        return self


class ExitObservation(OutcomeEvidence):
    lifecycle_id: str = Field(min_length=1)
    sessions: tuple[ExitSession, ...]
    forecasts: tuple[ExitForecast, ...]


class StandardExitTrade(EvaluationContract):
    signal_at: AwareDatetime
    executed_at: AwareDatetime
    exposure_sold: Decimal
    price: Decimal
    costs: Decimal
    net_proceeds: Decimal


class ExitPolicyPath(EvaluationContract):
    current_exposure: Decimal
    pending_target: Decimal
    trades: tuple[StandardExitTrade, ...]
    daily_wealth: tuple[Decimal, ...]
    dates: tuple[AwareDatetime, ...]
    net_total_return: Decimal | None
    gross_total_return: Decimal | None
    maximum_drawdown: Decimal
    exit_cost: Decimal
    turnover: Decimal


class ExitDecisionLabel(EvaluationContract):
    prediction_id: str
    cutoff_at: AwareDatetime
    state: str
    direction: Literal["HOLD", "REDUCE", "EXIT", "ABSTAIN", "DATA_FAILED"]
    statistical: bool
    downside_return: Decimal | None = None
    value_return: Decimal | None = None
    upside_capture: Decimal | None = None
    reversal_without_evidence: bool = False
    forecast: ExitForecast


class ExitPositionResult(StandardExitPosition):
    initial_quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    observation: ExitObservation | None
    policies: dict[str, ExitPolicyPath]
    decisions: tuple[ExitDecisionLabel, ...]
    missing_reasons: tuple[str, ...]
    replay_consistent: bool = True


class ExitForecastFreeze(EvaluationContract):
    lifecycle_id: str = Field(min_length=1)
    forecast: ExitForecast


class ExitEvaluationCommand(EvaluationContract):
    operation: Literal["REGISTER", "FREEZE", "OBSERVE", "EVALUATE"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    cutoff_at: AwareDatetime
    policy: ExitPolicy | None
    registration_event_id: str | None
    source_standard_event_id: str | None
    previous_event_id: str | None
    observations: tuple[ExitObservation, ...]
    forecast_freezes: tuple[ExitForecastFreeze, ...] = ()
    theses: tuple[ExitThesis, ...] = ()

    @model_validator(mode="after")
    def validate_operation(self) -> "ExitEvaluationCommand":
        if self.operation == "REGISTER":
            if (
                self.policy is None
                or self.registration_event_id
                or self.source_standard_event_id
                or self.previous_event_id
                or self.observations
                or self.forecast_freezes
                or self.theses
            ):
                raise ValueError("exit preregistration precedes observed outcomes")
        elif self.policy is not None or not self.registration_event_id:
            raise ValueError("exit evaluation requires retained preregistration")
        if self.operation in {"FREEZE", "OBSERVE"} and not self.source_standard_event_id:
            raise ValueError("exit observation requires a committed standard entry source")
        if self.operation == "EVALUATE" and (self.source_standard_event_id or self.observations):
            raise ValueError("exit historical evaluation reads the complete retained population")
        if self.operation == "FREEZE":
            if not self.forecast_freezes or self.observations:
                raise ValueError("exit prediction freeze requires forecasts without outcomes")
        elif self.forecast_freezes or self.theses:
            raise ValueError("exit outcome upload cannot introduce frozen predictions")
        if len({item.lifecycle_id for item in self.observations}) != len(self.observations):
            raise ValueError("duplicate standard exit lifecycle")
        return self


class ExitGate(EvaluationContract):
    estimate: Decimal | None
    bound: Decimal | None
    direction: Literal["LOWER", "UPPER", "EXACT"]
    threshold: Decimal
    strict: bool
    applicable: bool = True
    passed: bool | None
    block_bounds: dict[str, Decimal | None]
    undefined_resamples: dict[str, int]


class ExitWatermark(EvaluationContract):
    mature_months: int
    position_count: int
    nonoverlapping_windows: int
    periods: int
    directions: dict[str, int]
    target_positions: dict[str, int]
    target_events: dict[str, int]
    target_nonevents: dict[str, int]
    sufficient: bool
    disposition: str


class ExitInference(EvaluationContract):
    disposition: str
    watermarks: dict[str, ExitWatermark]
    gates: dict[str, ExitGate]
    diagnostics: dict[str, CalibrationDiagnostics]
    pinball_loss: dict[str, Decimal | None]
    resampling: dict[str, dict[str, str | int]]
    missing_evidence: tuple[str, ...] = ()
    inference_unit: Literal["WHOLE_LIFECYCLE_COMMON_ENTRY_MONTH_BLOCKS"] = (
        "WHOLE_LIFECYCLE_COMMON_ENTRY_MONTH_BLOCKS"
    )


class ExitCounts(EvaluationContract):
    registered: int
    due: int
    evaluable: int
    due_missing: int
    immature: int


class ExitEvaluationReport(EvaluationContract):
    disposition: str
    policy: ExitPolicy
    registration_event_id: str | None
    cutoff_at: AwareDatetime
    previous_event_id: str | None = None
    previous_report_id: str | None = None
    report_version: int = 1
    positions: tuple[ExitPositionResult, ...] = ()
    forecast_freezes: tuple[ExitForecastFreeze, ...] = ()
    theses: tuple[ExitThesis, ...] = ()
    counts: ExitCounts | None = None
    inference: ExitInference | None = None
    actionable: Literal[False] = False
    authorization_granted: Literal[False] = False
