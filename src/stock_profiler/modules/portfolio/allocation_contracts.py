"""Frozen allocation contracts, separate from recommendation, confirmation and execution."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_profiler.modules.candidate_selection.calibrated_candidates import CalibratedMember
from stock_profiler.modules.portfolio.contracts import PersonalRiskBudget
from stock_profiler.modules.position_management.contracts import (
    PositionContract,
    PositionEvidence,
    ReconciledPositionSnapshot,
)
from stock_profiler.modules.position_management.execution_contracts import ExecutionPlanCommand


class AllocationPolicy(PositionContract):
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    version_id: str = Field(min_length=1)
    entry_target_ratio: Decimal = Field(gt=0, lt=1)
    correlation_ceiling: Decimal = Field(gt=0, lt=1)
    neighborhood_ratio: Decimal = Field(gt=0, lt=1)
    turnover_ratio: Decimal = Field(gt=0, lt=1)
    correlation_window: int = Field(ge=2)


class AllocationSecurity(PositionContract):
    security_id: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    median_turnover: Decimal = Field(ge=0)
    turnover_window_sessions: Literal[20]
    evidence: PositionEvidence


class AcquisitionRoute(PositionContract):
    security_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    permission: bool
    price_cap: Decimal | None = Field(gt=0)
    price_cap_confirmed: bool
    current_price: Decimal | None = Field(default=None, gt=0)
    price_tick: Decimal = Field(gt=0)
    minimum_price: Decimal = Field(gt=0)
    maximum_price: Decimal = Field(gt=0)
    minimum_quantity: Decimal = Field(gt=0)
    quantity_increment: Decimal = Field(gt=0)
    commission_ratio: Decimal = Field(ge=0, lt=1)
    minimum_commission: Decimal = Field(ge=0)
    other_cost_ratio: Decimal = Field(ge=0, lt=1)
    disposal_friction_ratio: Decimal = Field(ge=0, lt=1)
    version_id: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    evidence: PositionEvidence

    def cost(self, principal: Decimal) -> Decimal:
        if principal == 0:
            return Decimal(0)
        return (
            max(self.minimum_commission, principal * self.commission_ratio)
            + principal * self.other_cost_ratio
        )


class AllocationCommitment(PositionContract):
    commitment_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    security_id: str = Field(min_length=1)
    issuer_id: str = Field(min_length=1)
    broker_order_id: str | None
    broker_order_ids: tuple[str, ...] = Field(default=(), exclude_if=lambda v: not v)
    broker_order_bindings: tuple[tuple[str, str], ...] = Field(
        default=(), exclude_if=lambda v: not v
    )
    principal: Decimal = Field(ge=0)
    quantity: Decimal | None = Field(default=None, gt=0)
    price_cap: Decimal | None = Field(default=None, gt=0)
    purchase_cost: Decimal = Field(ge=0)
    reconciliation_cash_hold: Decimal = Field(default=Decimal(0), ge=0, exclude_if=lambda v: v == 0)
    disposal_friction: Decimal = Field(ge=0)
    evidence: PositionEvidence

    @property
    def order_keys(self) -> tuple[tuple[str, str], ...]:
        if self.broker_order_bindings:
            return self.broker_order_bindings
        ids = (self.broker_order_id,) if self.broker_order_id is not None else self.broker_order_ids
        return tuple((self.account_id, identity) for identity in ids)

    @model_validator(mode="after")
    def distinct_order_bindings(self) -> "AllocationCommitment":
        if (
            sum(
                bool(value)
                for value in (
                    self.broker_order_id,
                    self.broker_order_ids,
                    self.broker_order_bindings,
                )
            )
            > 1
        ):
            raise ValueError("only one broker order reference representation is allowed")
        if any(not account or not order for account, order in self.order_keys):
            raise ValueError("broker order identities must be nonempty")
        if len(set(self.order_keys)) != len(self.order_keys):
            raise ValueError("broker order identities must be unique")
        return self


class AllocationCorrelations(PositionContract):
    version_id: str = Field(min_length=1)
    market_calendar_version: str = Field(min_length=1)
    return_semantics: Literal["DAILY_ADJUSTED"]
    market_dates: tuple[date, ...]
    returns: dict[str, tuple[Decimal, ...]]
    evidence: PositionEvidence


class CandidateAllocationCommand(PositionContract):
    contract_version: Literal["1.0.0"]
    operation: Literal["CANDIDATE_ALLOCATION"]
    cutoff_at: AwareDatetime
    risk_handoff: ExecutionPlanCommand
    candidate_event_id: str = Field(min_length=1)
    policy: AllocationPolicy | None = None
    securities: tuple[AllocationSecurity, ...] = ()
    routes: tuple[AcquisitionRoute, ...] = ()
    correlations: AllocationCorrelations | None = None
    commitments: tuple[AllocationCommitment, ...] = ()

    @model_validator(mode="after")
    def distinct_inputs(self) -> "CandidateAllocationCommand":
        if self.cutoff_at != self.risk_handoff.cutoff_at:
            raise ValueError("allocation and risk cutoff must match")
        for identities in (
            tuple(row.security_id for row in self.securities),
            tuple((row.security_id, row.account_id) for row in self.routes),
            tuple(row.commitment_id for row in self.commitments),
            tuple(key for row in self.commitments for key in row.order_keys),
        ):
            if len(set(identities)) != len(identities):
                raise ValueError("allocation inputs must have unique identities")
        return self


class AcquisitionLeg(PositionContract):
    route: AcquisitionRoute
    quantity: Decimal
    principal: Decimal
    purchase_cost: Decimal


AllocationCriterion = Literal[
    "COVERAGE",
    "COMPLETION_RATIOS",
    "PRINCIPAL",
    "PROBABILITIES",
    "PURCHASE_COST",
    "ORDER_COUNT",
    "STABLE_IDENTITIES",
]


class AllocationComparison(PositionContract):
    criterion: AllocationCriterion
    direction: Literal["MAXIMIZE", "MINIMIZE"]
    selected_value: tuple[Decimal, ...]
    alternative_value: tuple[Decimal, ...] | None
    relation: Literal["EQUAL", "WORSE", "NOT_REACHED", "INFEASIBLE"]


class AllocationRouteFailure(PositionContract):
    route: AcquisitionRoute
    reasons: tuple[str, ...]


class AllocationCapacityCheck(PositionContract):
    gate_id: str
    reason: str
    security_ids: tuple[str, ...]
    account_id: str | None = None
    committed_margin: Decimal
    available_before: Decimal
    remaining_after_continuous: Decimal
    remaining_after_plan: Decimal


class CandidateAllocationRow(PositionContract):
    candidate: CalibratedMember
    issuer_id: str
    committed_exposure: Decimal
    target_gap: Decimal
    continuous_principal: Decimal
    principal: Decimal
    outcome: Literal["FULLY_ALLOCATED", "PARTIALLY_ALLOCATED", "UNALLOCATED"]
    reasons: tuple[str, ...]
    primary_reason: str | None = None
    comparisons: tuple[AllocationComparison, ...] = ()
    route_failures: tuple[AllocationRouteFailure, ...] = ()
    legs: tuple[AcquisitionLeg, ...] = ()


class CandidateAllocationOutcome(PositionContract):
    contract_version: Literal["1.0.0"] = "1.0.0"
    disposition: Literal["BLOCKED", "PLANNED", "AWAITING_PRICE_CAP"]
    reasons: tuple[str, ...]
    rows: tuple[CandidateAllocationRow, ...] = ()
    actionable: Literal[False] = False
    plan_id: str | None = None
    formed_at: AwareDatetime | None = None
    candidate_event_id: str | None = None
    candidate_batch_id: str | None = None
    candidate_conclusion_version: str | None = None
    policy: AllocationPolicy | None = None
    position_snapshot_id: str | None = None
    position_snapshot: ReconciledPositionSnapshot | None = None
    risk_budget: PersonalRiskBudget | None = None
    risk_budget_version_id: str | None = None
    total_principal: Decimal = Decimal(0)
    remaining_cash: Decimal | None = None
    risk_handoff: ExecutionPlanCommand | None = None
    correlations: AllocationCorrelations | None = None
    commitments: tuple[AllocationCommitment, ...] = ()
    securities: tuple[AllocationSecurity, ...] = ()
    routes: tuple[AcquisitionRoute, ...] = ()
    purchase_sequence: tuple[str, ...] = ()
    eligibility_evidence_ids: tuple[str, ...] = ()
    discrete_objectives: tuple[tuple[Decimal, ...], ...] = ()
    capacity_checks: tuple[AllocationCapacityCheck, ...] = ()
