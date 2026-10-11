"""Pure directional classification from qualified guarded forecast bounds."""

from dataclasses import dataclass
from typing import Literal

from stock_profiler.modules.position_management.action_contracts import PositionActionPolicy
from stock_profiler.modules.position_management.exit_contracts import GuardedExitProbability


@dataclass(frozen=True)
class StatisticalActionAssessment:
    direction: Literal["HOLD", "REDUCE", "EXIT"] | None
    evidence_missing: bool


def assess_statistical_action(
    policy: PositionActionPolicy,
    probabilities: tuple[GuardedExitProbability, ...],
    grid: Literal["0.05", "0.10"],
) -> StatisticalActionAssessment:
    down = next((p for p in probabilities if p.target == "D20" and p.loss_boundary == grid), None)
    value = next((p for p in probabilities if p.target == "V60"), None)
    if value is None or value.guarded_lower is None or value.guarded_upper is None:
        return StatisticalActionAssessment(None, True)
    if value.guarded_upper <= policy.value_denied_upper:
        return StatisticalActionAssessment("EXIT", False)
    if down is None or down.guarded_lower is None or down.guarded_upper is None:
        return StatisticalActionAssessment(None, True)
    if down.guarded_lower >= policy.downside_risky_lower:
        return StatisticalActionAssessment("REDUCE", False)
    if down.guarded_upper <= policy.downside_safe_upper and (
        value.guarded_lower >= policy.value_established_lower
    ):
        return StatisticalActionAssessment("HOLD", False)
    return StatisticalActionAssessment(None, False)
