"""Wire authenticated candidate intent to saved host facts and the case runner."""

from typing import Any

from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.foundation.clock import Clock
from stock_profiler.modules.decision_cases.domain import DecisionCaseExecution
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.delivery.candidate_commands import (
    CandidateWorkspaceCommand,
    resolve_candidate_command,
)


def submit_candidate_command(
    settings: Settings,
    principal: AccessPrincipal | None,
    payload: dict[str, Any],
    *,
    clock: Clock | None = None,
) -> DecisionCaseExecution:
    case = resolve_candidate_command(
        CandidateWorkspaceCommand.model_validate(payload),
        principal,
        ResultDelivery.from_settings(settings, clock=clock),
    )
    return run_frozen_decision_case(settings, case.model_dump(mode="json"), clock=clock)
