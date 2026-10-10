"""Current saved-input checks shared by confirmation and replanning recovery."""

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.portfolio.allocation_inputs import (
    allocation_inputs_match,
    is_allocation_input_source,
)


def saved_allocation_inputs_changed(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> bool:
    confirmation = case.candidate_confirmation
    command = confirmation.revalidation if confirmation is not None else case.candidate_allocation
    assert command is not None and case.access_scope is not None
    source_plan_event_id = (
        confirmation.plan_event_id if confirmation is not None else command.replaces_plan_event_id
    )
    assert source_plan_event_id is not None
    latest = next(
        (
            fact
            for fact in reversed(ledger.candidate_allocation_history(connection, case.access_scope))
            if fact.case.candidate_allocation is not None
            and fact.case.candidate_allocation.candidate_event_id == command.candidate_event_id
            and is_allocation_input_source(
                fact.case.candidate_allocation, fact.result.candidate_allocation
            )
        ),
        None,
    )
    if latest is None or latest.decision_event_id == source_plan_event_id:
        return False
    assert latest.case.candidate_allocation is not None
    return not allocation_inputs_match(latest.case.candidate_allocation, command)
