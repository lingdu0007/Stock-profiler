"""Immutable security-only exit evidence and forward-only thesis contracts."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from stock_profiler.modules.qualification.contracts import CapabilityVersion, QualificationScope


class ExitContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExitProposition(ExitContract):
    proposition_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    authority: Literal["DISCLOSURE", "EXCHANGE"]
    source: str = Field(min_length=1)
    operator: Literal["LE", "GE", "EQ"]
    threshold: Decimal = Field(allow_inf_nan=False)
    trigger_direction: Literal["LIQUIDATE"]


class ExitThesis(ExitContract):
    thesis_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    lifecycle_id: str = Field(min_length=1)
    origin: Literal["SYSTEM", "EXTERNAL"]
    registered_at: AwareDatetime
    valid_until: AwareDatetime
    propositions: tuple[ExitProposition, ...] = Field(min_length=1)


class ThesisRegistration(ExitContract):
    operation: Literal["REGISTER_THESIS"]
    contract_version: Literal["1.0.0"]
    cutoff_at: AwareDatetime
    position_event_id: str
    position_id: str
    thesis: ExitThesis


class SecurityEvidence(ExitContract):
    evidence_id: str = Field(min_length=1)
    family: Literal["SECURITY", "MARKET", "RISK"]
    security_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    authority: Literal["DISCLOSURE", "EXCHANGE", "MODEL", "NEWS"]
    source_version: str = Field(min_length=1)
    public_at: AwareDatetime
    acquired_at: AwareDatetime
    validated_at: AwareDatetime
    valid_until: AwareDatetime
    complete: bool
    metric: str = Field(min_length=1)
    value: Decimal = Field(allow_inf_nan=False)
    legal_termination: bool


class ExitCalibration(ExitContract):
    target: Literal["D20", "V60"]
    loss_boundary: Literal["0.05", "0.10", "0.15", "0.20"] | None
    market_state: str = Field(min_length=1)
    band_lower: Decimal = Field(ge=0, le=1)
    band_upper: Decimal = Field(ge=0, le=1)
    lower_error: Decimal = Field(ge=0, le=1)
    upper_error: Decimal = Field(ge=0, le=1)
    version: CapabilityVersion
    calendar_version: str = Field(min_length=1)


class ExitPrediction(ExitContract):
    security_id: str = Field(min_length=1)
    assessment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    point: Decimal = Field(ge=0, le=1)
    produced_at: AwareDatetime
    valid_until: AwareDatetime
    qualification_scope: QualificationScope
    qualification_id: str = Field(min_length=1)
    calibration: ExitCalibration


class GuardedExitProbability(ExitContract):
    security_id: str
    assessment_digest: str
    target: Literal["D20", "V60"]
    loss_boundary: str | None
    point: Decimal
    guarded_lower: Decimal | None
    guarded_upper: Decimal | None
    qualification_status: Literal["VALID", "AT_RISK", "NOT_QUALIFIED"]
    qualification_id: str
    qualification_scope: QualificationScope
    version: CapabilityVersion
    produced_at: AwareDatetime
    valid_until: AwareDatetime
    calibration: ExitCalibration
    reasons: tuple[str, ...] = ()


class SecurityAssessment(ExitContract):
    operation: Literal["ASSESS_SECURITY"]
    contract_version: Literal["1.0.0"]
    cutoff_at: AwareDatetime
    security_id: str = Field(min_length=1)
    lifecycle_id: str = Field(min_length=1)
    thesis_event_id: str | None
    market_state: str = Field(min_length=1)
    board: str = Field(min_length=1)
    standard_price: Decimal = Field(gt=0, allow_inf_nan=False)
    calendar_version: str = Field(min_length=1)
    evidence: tuple[SecurityEvidence, ...]
    version: CapabilityVersion | None = Field(default=None, exclude_if=lambda value: value is None)
    predictions: tuple[ExitPrediction, ...]


class ExitTarget(ExitContract):
    target: Literal["D20", "V60"]
    market_sessions: Literal[20, 60]
    return_basis: Literal["NET_TOTAL_RETURN"] = "NET_TOTAL_RETURN"
    aggregation: Literal["DAILY_MINIMUM", "TERMINAL_INCREMENT_VS_EXIT_CASH"]
    cash_return: Literal["0"] = "0"


class ExitHardGate(ExitContract):
    gate_id: str
    direction: Literal["LIQUIDATE"] = "LIQUIDATE"
    evidence_id: str
    authority: Literal["DISCLOSURE", "EXCHANGE"]
    source: str
    thesis_event_id: str | None
    established_at: AwareDatetime


class ExitOutcome(ExitContract):
    disposition: Literal["THESIS_REGISTERED", "ASSESSED", "NON_ACTIONABLE", "BLOCKED"]
    reasons: tuple[str, ...] = ()
    thesis: ExitThesis | None = None
    thesis_event_id: str | None = None
    hard_gates: tuple[ExitHardGate, ...] = ()
    standard_price: Decimal | None = None
    targets: tuple[ExitTarget, ...] = ()
    security_id: str | None = None
    evidence: tuple[SecurityEvidence, ...] = ()
    probabilities: tuple[GuardedExitProbability, ...] = ()
