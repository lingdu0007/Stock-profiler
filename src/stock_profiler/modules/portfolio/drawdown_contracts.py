"""Explicit synthetic policy and retained capital-epoch facts."""

from decimal import Decimal
from fractions import Fraction
from typing import Literal

from pydantic import AwareDatetime, Field

from stock_profiler.modules.portfolio.contracts import DrawdownBudget, PortfolioContract
from stock_profiler.modules.position_management.contracts import PositionEvidence


class DrawdownPolicy(PortfolioContract):
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    version_id: str = Field(min_length=1)
    defensive_exposure_ratio: Decimal = Field(gt=0, lt=1)
    caution_recovery_ratio: Decimal = Field(gt=0, lt=1)
    defensive_recovery_ratio: Decimal = Field(gt=0, lt=1)
    caution_recovery_sessions: int = Field(gt=0)
    defensive_recovery_sessions: int = Field(gt=0)
    cooling_sessions: int = Field(gt=0)
    market_calendar_version_id: str = Field(min_length=1)


class DrawdownValuation(PortfolioContract):
    position_event_id: str = Field(min_length=1)
    liquidation_cost: Decimal | None = Field(ge=0)
    evidence: PositionEvidence


class DrawdownRecoveryGate(PortfolioContract):
    hard_gate_active: bool
    evidence: PositionEvidence


class FlowLedgerKey(PortfolioContract):
    account_id: str = Field(min_length=1)
    entry_id: str = Field(min_length=1)


class CapitalFlow(PortfolioContract):
    flow_id: str = Field(min_length=1)
    kind: Literal["EXTERNAL", "INTERNAL"]
    occurred_at: AwareDatetime
    before_valuation: DrawdownValuation
    ledger_keys: tuple[FlowLedgerKey, ...] = Field(min_length=1)


class ExactRatio(PortfolioContract):
    numerator: int
    denominator: int = Field(gt=0)

    @classmethod
    def from_fraction(cls, value: Fraction) -> "ExactRatio":
        return cls(numerator=value.numerator, denominator=value.denominator)

    @property
    def fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)


class CapitalReauthorization(PortfolioContract):
    confirmation_id: str = Field(min_length=1)
    authorization_id: str = Field(min_length=1)
    previous_epoch_id: str = Field(min_length=1)
    epoch_id: str = Field(min_length=1)
    confirmed_at: AwareDatetime
    confirmed: Literal[True]


class DrawdownCommand(PortfolioContract):
    contract_version: Literal["1.0.0"]
    operation: Literal["OPEN", "OBSERVE", "CLOSE", "REAUTHORIZE"]
    portfolio_id: str = Field(min_length=1)
    authorization_id: str = Field(min_length=1)
    epoch_id: str = Field(min_length=1)
    previous_decision_id: str | None
    cutoff_at: AwareDatetime
    valuation: DrawdownValuation
    policy: DrawdownPolicy
    market_session_ordinal: int | None = Field(default=None, ge=1)
    other_risk_gate: DrawdownRecoveryGate | None = None
    capital_flows: tuple[CapitalFlow, ...] = ()
    reauthorization: CapitalReauthorization | None = None


class DrawdownState(PortfolioContract):
    decision_id: str
    epoch_id: str
    portfolio_id: str
    authorization_id: str
    account_ids: tuple[str, ...]
    cutoff_at: AwareDatetime
    accounting_cutoff_at: AwareDatetime
    policy: DrawdownPolicy
    thresholds: DrawdownBudget
    net_liquidation_equity: Decimal | None
    units: Decimal
    unit_nav: Decimal | None
    high_water_nav: Decimal
    maximum_drawdown: Decimal
    current_drawdown: Decimal | None
    risk_state: Literal["NORMAL", "CAUTION", "DEFENSIVE", "PRESERVATION"]
    new_exposure_blocked: bool
    valuation: DrawdownValuation
    current_stock_exposure: Decimal | None
    stock_exposure_limit: Decimal | None
    risk_direction: Literal["REDUCE", "EXIT"] | None
    execution_blocked: bool
    stock_exposure_target_value: Decimal | None
    recovery_sessions: int = 0
    last_recovery_session: int | None = None
    exact_units: ExactRatio
    exact_peak: ExactRatio
    processed_transfers: tuple[FlowLedgerKey, ...]
    processed_flow_ids: tuple[str, ...] = ()
    epoch_status: Literal["OPEN", "CLOSED"] = "OPEN"
    closed_at: AwareDatetime | None = None
    cooling_sessions: int = 0
    previous_epoch_id: str | None = None
    reauthorization: CapitalReauthorization | None = None


class DrawdownOutcome(PortfolioContract):
    disposition: Literal["ACCEPTED", "DENIED", "UNKNOWN"]
    reasons: tuple[str, ...]
    state: DrawdownState | None = None
