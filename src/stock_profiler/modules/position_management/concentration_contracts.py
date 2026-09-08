"""Explicit inputs and saved results for deterministic issuer protection."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_profiler.modules.portfolio.contracts import PortfolioUseCommand, TwoThresholdBudget
from stock_profiler.modules.position_management.contracts import (
    PositionContract,
    PositionEvidence,
    PositionSnapshotCommand,
)


class AccountLiquidationCost(PositionContract):
    account_id: str = Field(min_length=1)
    amount: Decimal | None = Field(ge=0)
    evidence: PositionEvidence


class ConcentrationCommand(PositionContract):
    operation: Literal["ISSUER_CONCENTRATION_ASSESS"]
    contract_version: Literal["1.0.0"]
    authorization: PortfolioUseCommand
    position_snapshot: PositionSnapshotCommand
    liquidation_costs: tuple[AccountLiquidationCost, ...]

    @model_validator(mode="after")
    def validate_protection_request(self) -> ConcentrationCommand:
        if self.authorization.requested_action != "DETERMINISTIC_PROTECTION":
            raise ValueError("concentration assessment requires deterministic protection")
        ids = tuple(cost.account_id for cost in self.liquidation_costs)
        if len(ids) != len(set(ids)):
            raise ValueError("liquidation costs must have unique accounts")
        if not set(ids).issubset(self.position_snapshot.account_ids):
            raise ValueError("liquidation costs must belong to the snapshot")
        return self


class ConcentrationTarget(PositionContract):
    security_id: str
    target_quantity: Decimal
    required_reduction_quantity: Decimal | None


class ConcentrationQuantityBasis(PositionContract):
    account_id: str
    security_id: str
    quantity: Decimal
    cutoff_at: AwareDatetime


class IssuerConcentration(PositionContract):
    issuer_id: str
    current_market_exposure: Decimal | None
    position_weight: Decimal | None
    state: Literal["NORMAL", "BUFFER", "REMEDIATION_REQUIRED", "RESOLVED", "UNKNOWN"]
    new_exposure_blocked: bool
    direction: Literal["REDUCE"] | None
    obligation_id: str | None
    obligation_started_at: AwareDatetime | None = None
    obligation_risk_budget_version_id: str | None = None
    obligation_thresholds: TwoThresholdBudget | None = None
    obligation_quantity_basis: tuple[ConcentrationQuantityBasis, ...] = ()
    targets: tuple[ConcentrationTarget, ...]
    exposure_gap: Decimal | None
    execution_blocked: bool


class ConcentrationOutcome(PositionContract):
    disposition: Literal["ASSESSED", "BLOCKED"]
    reasons: tuple[str, ...]
    portfolio_id: str
    snapshot_id: str
    cutoff_at: AwareDatetime
    valuation_currency: str
    portfolio_net_liquidation_equity: Decimal | None
    risk_budget_version_id: str | None
    thresholds: TwoThresholdBudget | None
    issuers: tuple[IssuerConcentration, ...]


@dataclass(frozen=True)
class ConcentrationHistory:
    """Chronological scope-valid original facts plus a detail-free negative scope guard."""

    outcomes: tuple[ConcentrationOutcome, ...] = ()
    uncovered_obligation: bool = False
