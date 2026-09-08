"""Frozen execution-plan identities, separate from orders and risk discharge."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_profiler.modules.position_management.contracts import PositionContract, PositionEvidence


class DisposalDividendLot(PositionContract):
    lot_id: str = Field(min_length=1)
    quantity: Decimal = Field(gt=0)
    tax_per_share: Decimal = Field(ge=0)


class DisposalCostCurve(PositionContract):
    version_id: str = Field(min_length=1)
    commission_ratio: Decimal = Field(ge=0, lt=1)
    minimum_commission: Decimal = Field(ge=0)
    trading_fee_ratio: Decimal = Field(ge=0, lt=1)
    settlement_fee_ratio: Decimal = Field(ge=0, lt=1)
    tax_ratio: Decimal = Field(ge=0, lt=1)
    slippage_ratio: Decimal = Field(ge=0, lt=1)
    market_impact_ratio: Decimal = Field(ge=0, lt=1)
    dividend_lots: tuple[DisposalDividendLot, ...]
    evidence: PositionEvidence

    def cost(self, quantity: Decimal, price: Decimal) -> Decimal:
        if quantity == 0:
            return Decimal(0)
        gross = quantity * price
        remaining = quantity
        dividend_tax = Decimal(0)
        for lot in self.dividend_lots:
            used = min(remaining, lot.quantity)
            dividend_tax += used * lot.tax_per_share
            remaining -= used
        return (
            max(self.minimum_commission, gross * self.commission_ratio)
            + gross
            * (
                self.trading_fee_ratio
                + self.settlement_fee_ratio
                + self.tax_ratio
                + self.slippage_ratio
                + self.market_impact_ratio
            )
            + dividend_tax
        )


class ExecutionRoute(PositionContract):
    account_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    minimum_quantity: Decimal = Field(gt=0)
    quantity_increment: Decimal = Field(gt=0)
    allow_full_odd_lot: bool
    first_sellable_at: AwareDatetime
    transferable_at: AwareDatetime
    rules_evidence: PositionEvidence
    cost_curve: DisposalCostCurve | None
    conservative_cost_curve: DisposalCostCurve | None = None

    def qualified_curve(self, cutoff_at: AwareDatetime) -> DisposalCostCurve | None:
        for curve in (self.cost_curve, self.conservative_cost_curve):
            if curve is not None and not curve.evidence.problem_codes(
                cutoff_at, require_current_completeness=True
            ):
                return curve
        return None


class EstablishedQuantityTarget(PositionContract):
    target_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    target_quantity: Decimal = Field(ge=0)
    direction: Literal["REDUCE", "EXIT"]
    qualified: bool
    policy_version: str = Field(min_length=1)
    evidence: PositionEvidence

    @model_validator(mode="after")
    def validate_direction(self) -> "EstablishedQuantityTarget":
        if (self.direction == "EXIT") != (self.target_quantity == 0):
            raise ValueError("zero quantity requires an explicit exit target")
        return self


class ExecutionPlanCommand(PositionContract):
    contract_version: Literal["1.0.0"]
    operation: Literal["EXECUTION_PLAN"]
    portfolio_id: str = Field(min_length=1)
    authorization_id: str = Field(min_length=1)
    cutoff_at: AwareDatetime
    concentration_event_id: str = Field(min_length=1)
    stress_event_id: str = Field(min_length=1)
    liquidity_event_id: str = Field(min_length=1)
    drawdown_event_id: str = Field(min_length=1)
    routes: tuple[ExecutionRoute, ...] = ()
    established_targets: tuple[EstablishedQuantityTarget, ...] = ()


class ExecutionTarget(PositionContract):
    security_id: str
    target_quantity: Decimal
    required_sale_quantity: Decimal | None
    source_obligation_ids: tuple[str, ...]
    remaining_quantity: Decimal | None = None
    remaining_gap: Decimal | None = None
    direction: Literal["HOLD", "REDUCE", "EXIT"]
    rounding_induced_full_sale: bool = False


class ExecutionLeg(PositionContract):
    account_id: str
    security_id: str
    quantity: Decimal
    gross_proceeds: Decimal
    disposal_cost: Decimal
    net_proceeds: Decimal
    route: ExecutionRoute


class ExecutionPlanOutcome(PositionContract):
    disposition: Literal["BLOCKED", "PLANNED", "RECONFIRMATION_REQUIRED"]
    reasons: tuple[str, ...]
    new_exposure_blocked: bool
    risk_restored: Literal[False] = False
    legs: tuple[ExecutionLeg, ...] = ()
    targets: tuple[ExecutionTarget, ...] = ()
    requires_confirmation: bool = False
    initial_target_sale_value: Decimal | None = None
    projected_stress_gap: Decimal | None = None
    projected_cash_gap: Decimal | None = None
    projected_exposure_gap: Decimal | None = None
    cost_routing_basis: Literal["VERIFIED_FULL_COST", "CONSERVATIVE_BOUND_PROPORTIONAL"] | None = (
        None
    )
