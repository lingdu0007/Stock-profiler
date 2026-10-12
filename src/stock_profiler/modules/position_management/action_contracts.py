"""Frozen position conclusions, kept separate from security evidence and fills."""

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from stock_profiler.modules.position_management.contracts import PositionContract
from stock_profiler.modules.position_management.execution_contracts import (
    ExecutionPlanCommand,
    ExecutionPlanOutcome,
)
from stock_profiler.modules.qualification.contracts import CapabilityVersion, QualificationScope


class PositionActionPolicy(PositionContract):
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    version: CapabilityVersion
    normal_grid: Literal["0.10"]
    risk_grid: Literal["0.05"]
    downside_safe_upper: Decimal = Field(ge=0, lt=1)
    downside_risky_lower: Decimal = Field(gt=0, le=1)
    value_established_lower: Decimal = Field(gt=0, le=1)
    value_denied_upper: Decimal = Field(ge=0, lt=1)
    reduction_ratio: Decimal = Field(gt=0, lt=1)
    lifecycle_cost_ratio: Decimal = Field(gt=0, lt=1)
    maximum_cost_ratio: Decimal = Field(ge=0, lt=1)
    maximum_minimum_commission: Decimal = Field(ge=0)
    maximum_dividend_tax_per_share: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def disjoint_bands(self) -> "PositionActionPolicy":
        if self.downside_safe_upper >= self.downside_risky_lower:
            raise ValueError("downside bands must be disjoint")
        if self.value_denied_upper >= self.value_established_lower:
            raise ValueError("value bands must be disjoint")
        return self


class PositionActionCommand(PositionContract):
    contract_version: Literal["1.0.0"]
    operation: Literal["POSITION_ACTION"]
    cutoff_at: AwareDatetime
    security_id: str = Field(min_length=1)
    lifecycle_id: str = Field(min_length=1)
    security_event_id: str = Field(min_length=1)
    execution: ExecutionPlanCommand
    policy: PositionActionPolicy
    qualification_scope: QualificationScope
    qualification_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def host_owned_targets(self) -> "PositionActionCommand":
        if self.execution.established_targets:
            raise ValueError("position action targets must come from committed host evidence")
        return self


class StatisticalReductionAnchor(PositionContract):
    event_id: str
    quantity: Decimal = Field(gt=0)
    market_value: Decimal = Field(gt=0)
    frozen_at: AwareDatetime


class PositionActionOutcome(PositionContract):
    disposition: Literal["CONCLUDED", "ABSTAINED", "DATA_OBSERVATION", "BLOCKED"]
    security_id: str
    lifecycle_id: str
    direction: Literal["HOLD", "REDUCE", "EXIT"] | None = None
    model_direction: Literal["HOLD", "REDUCE", "EXIT"] | None = None
    target_quantity: Decimal | None = None
    current_quantity: Decimal | None = None
    selected_grid: Literal["0.05", "0.10"] | None = None
    reasons: tuple[str, ...] = ()
    buy_authorized: Literal[False] = False
    risk_restored: Literal[False] = False
    deterministic_execution: ExecutionPlanOutcome | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    execution: ExecutionPlanOutcome | None = None
    anchor: StatisticalReductionAnchor | None = None
    cost_anchor: StatisticalReductionAnchor | None = None
