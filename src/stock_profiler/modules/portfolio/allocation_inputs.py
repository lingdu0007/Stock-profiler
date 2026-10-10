"""Separate accepted allocation inputs from rejected derived planning attempts."""

from stock_profiler.modules.portfolio.allocation_contracts import (
    CandidateAllocationCommand,
    CandidateAllocationOutcome,
)


def is_allocation_input_source(
    command: CandidateAllocationCommand,
    outcome: CandidateAllocationOutcome | None,
) -> bool:
    return outcome is not None and (
        command.replaces_plan_event_id is None or outcome.disposition == "PLANNED"
    )


def allocation_inputs_match(
    left: CandidateAllocationCommand,
    right: CandidateAllocationCommand,
) -> bool:
    lineage = {"replaces_plan_event_id", "replanning_reason"}
    return left.model_dump(exclude=lineage) == right.model_dump(exclude=lineage)
