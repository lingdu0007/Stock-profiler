"""One current permission check shared by commit and saved-plan delivery."""

from datetime import datetime

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.qualification.beta import (
    envelope_decision,
    observation_history_reasons,
    qualification_reasons,
)
from stock_profiler.modules.qualification.beta_contracts import BetaEnvelopeOutcome


def saved_beta_permission_reasons(
    case: FrozenDecisionCase,
    saved: BetaEnvelopeOutcome | None,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    now: datetime,
) -> tuple[str, ...]:
    command = case.candidate_allocation or (
        case.candidate_confirmation.revalidation if case.candidate_confirmation else None
    )
    if command is None or command.beta is None:
        return ()
    assert case.access_scope is not None
    history = ledger.governance_history(connection, case.access_scope)
    reasons = qualification_reasons(
        command.beta, history, now, evidence_cutoff=command.cutoff_at
    ) or observation_history_reasons(
        command.beta,
        tuple(
            fact.case.candidate_allocation.beta
            for fact in ledger.candidate_allocation_history(connection, case.access_scope)
            if fact.case.candidate_allocation is not None
            and fact.case.candidate_allocation.beta is not None
        ),
        now,
    )
    if reasons:
        return reasons
    envelope, observation_reasons, _, enabled = envelope_decision(
        command.beta, now, history, evidence_cutoff=command.cutoff_at
    )
    if not enabled or saved is None or saved.permission_envelope != envelope:
        return observation_reasons or ("BETA_PERMISSION_CHANGED",)
    return ()
