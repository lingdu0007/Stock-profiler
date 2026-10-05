"""Versioned synthetic standard evaluation inputs and saved report results."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from stock_profiler.modules.delivery.candidate_reminder_contracts import CandidateReminderRecord


class EvaluationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TradingCosts(EvaluationContract):
    commission: Decimal = Field(ge=0, allow_inf_nan=False)
    fees: Decimal = Field(ge=0, allow_inf_nan=False)
    taxes: Decimal = Field(ge=0, allow_inf_nan=False)
    slippage: Decimal = Field(ge=0, allow_inf_nan=False)

    @property
    def total(self) -> Decimal:
        return self.commission + self.fees + self.taxes + self.slippage


class OutcomeEvidence(EvaluationContract):
    evidence_id: str = Field(min_length=1)
    authority: Literal["EXCHANGE"]
    source_version: str = Field(min_length=1)
    effective_at: AwareDatetime
    published_at: AwareDatetime
    acquired_at: AwareDatetime
    validated_at: AwareDatetime
    corrects_evidence_id: str | None
    correction_reason: str | None

    @property
    def available_at(self) -> AwareDatetime:
        return max(self.published_at, self.acquired_at, self.validated_at)

    @model_validator(mode="after")
    def validate_clocks(self) -> "OutcomeEvidence":
        if self.acquired_at > self.validated_at or self.effective_at > self.available_at:
            raise ValueError("outcome evidence clocks are inconsistent")
        if bool(self.corrects_evidence_id) != bool(self.correction_reason):
            raise ValueError("authoritative correction requires target and reason")
        return self


class EntrySession(EvaluationContract):
    closed_at: AwareDatetime
    buyable: bool
    unavailable_reason: (
        Literal["SUSPENDED", "LIMIT_UP", "NO_SELLER_LIQUIDITY", "PURCHASE_EXCLUDED"] | None
    )
    turnover: Decimal = Field(ge=0, allow_inf_nan=False)
    volume: Decimal = Field(ge=0, allow_inf_nan=False)
    costs: TradingCosts | None

    @model_validator(mode="after")
    def validate_execution(self) -> "EntrySession":
        if self.buyable and (
            self.volume <= 0
            or self.turnover <= 0
            or self.costs is None
            or self.unavailable_reason is not None
        ):
            raise ValueError("buyable session requires VWAP and applicable costs")
        if not self.buyable and self.unavailable_reason is None:
            raise ValueError("nonbuyable session requires authoritative reason")
        return self


class EntryEvidence(OutcomeEvidence):
    sessions: tuple[EntrySession, ...] = Field(min_length=1, max_length=5)


class TerminalEvidence(OutcomeEvidence):
    price: Decimal = Field(ge=0, allow_inf_nan=False)
    quantity_multiplier: Decimal = Field(gt=0, allow_inf_nan=False)
    cash_distributions: Decimal = Field(ge=0, allow_inf_nan=False)
    actions_complete_through: AwareDatetime
    costs: TradingCosts


class StandardObservation(EvaluationContract):
    security_id: str = Field(min_length=1)
    entry: EntryEvidence | None
    terminal: TerminalEvidence | None


class StandardOutcomeCommand(EvaluationContract):
    contract_version: Literal["1.0.0"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    selection_event_id: str | None = Field(min_length=1)
    candidate_event_id: str | None = Field(default=None, min_length=1)
    cutoff_at: AwareDatetime
    market_calendar_version: str = Field(min_length=1)
    standard_quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    observations: tuple[StandardObservation, ...]
    previous_event_id: str | None

    @property
    def source_event_id(self) -> str:
        return self.selection_event_id or self.candidate_event_id or ""

    @model_validator(mode="after")
    def validate_source(self) -> "StandardOutcomeCommand":
        if (self.selection_event_id is None) == (self.candidate_event_id is None):
            raise ValueError("standard outcomes require exactly one retained source event")
        return self


class EvaluationRegistration(EvaluationContract):
    evaluation_id: str
    population: Literal["SELECTION", "PROBABILITY", "CANDIDATE"]
    source_event_id: str
    security_id: str
    frozen_probability: Decimal | None
    registered_at: AwareDatetime
    admission_contract: Literal["standard-evaluation.1.0.0"] = "standard-evaluation.1.0.0"
    admission_reason: str
    standard_quantity: Decimal = Decimal("100")
    market_calendar_version: str


class EvaluationMember(EvaluationContract):
    evaluation_id: str
    population: Literal["SELECTION", "PROBABILITY", "CANDIDATE"]
    source_event_id: str
    frozen_probability: Decimal | None = None
    inclusion_reason: str = "FROZEN_SELECTION_MEMBER"
    security_id: str
    matures_at: AwareDatetime
    state: Literal["PENDING", "UNAVAILABLE", "ACHIEVED", "NOT_ACHIEVED"]
    entry_at: AwareDatetime | None = None
    entry_price: Decimal | None = None
    net_total_return: Decimal | None = None
    entry_expired: bool = False
    observation: StandardObservation | None = None


class PopulationCounts(EvaluationContract):
    registered: int
    due: int
    evaluable: int
    missing: int
    immature: int
    achieved: int
    not_achieved: int
    formal_adjudication: Literal["INDETERMINATE", "EVIDENCE_COMPLETE"]


class CandidateBatchDelivery(EvaluationContract):
    event_id: str
    report_version_id: str | None
    plan_month: str
    disposition: str
    knowledge_cutoff: AwareDatetime
    generated_at: AwareDatetime
    committed_at: AwareDatetime
    published_at: AwareDatetime | None
    valid_from: AwareDatetime | None
    valid_through: AwareDatetime | None
    expired: bool | None
    corrects_event_id: str | None
    superseded_by_event_id: str | None
    reminders: tuple[CandidateReminderRecord, ...]


class StandardOutcomeReport(EvaluationContract):
    contract_version: Literal["1.0.0"] = "1.0.0"
    selection_event_id: str | None
    candidate_event_id: str | None
    cutoff_at: AwareDatetime
    previous_event_id: str | None
    members: tuple[EvaluationMember, ...]
    populations: dict[str, PopulationCounts]
    previous_report_id: str | None
    report_version: int = Field(ge=1)
    delivery: tuple[CandidateBatchDelivery, ...]
