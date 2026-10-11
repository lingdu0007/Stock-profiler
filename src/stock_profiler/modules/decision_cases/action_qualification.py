"""Exact current authorization and policy qualification at the host boundary."""

import json
from decimal import Decimal
from hashlib import sha256
from typing import Literal

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.position_management.contracts import PositionReconciliationOutcome
from stock_profiler.modules.position_management.exit_contracts import (
    ExitOutcome,
    SecurityAssessment,
)
from stock_profiler.modules.qualification.service import (
    current_qualification,
    qualification_is_current,
)


def check_action_qualification(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    assessment: SecurityAssessment,
    security: ExitOutcome,
    position: PositionReconciliationOutcome,
    grid: Literal["0.05", "0.10"],
) -> tuple[str, ...]:
    command, scope = case.position_action, case.access_scope
    assert command is not None and scope is not None
    units = [u for u in position.snapshot.action_units if u.security_id == command.security_id]
    reasons: list[str] = []
    authorizations = ledger.portfolio_authorization_history(
        connection, scope, command.execution.portfolio_id, case.knowledge_cutoff
    )
    latest = next(
        (
            outcome.authorization
            for outcome in reversed(authorizations)
            if outcome.authorization is not None
        ),
        None,
    )
    if (
        latest is None
        or latest.authorization_id != command.execution.authorization_id
        or not latest.evidence_available_by(command.cutoff_at)
        or latest.proposal.risk_budget.expires_at <= command.cutoff_at
        or latest.proposal.risk_budget.action_policy_version_id != command.policy.version.version_id
        or Decimal(grid) not in latest.proposal.risk_budget.downside_grid
    ):
        reasons.append("RISK_BUDGET_UNAVAILABLE_OR_EXPIRED")

    qualification = command.qualification_scope
    record = current_qualification(
        ledger.governance_history(connection, scope), qualification, command.policy.version
    )
    basis = (record.formal_passing_evidence or record.authorization_evidence) if record else None
    digest = sha256(
        json.dumps(
            command.policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    if (
        qualification.capability != "position-action"
        or qualification.purpose != "initial-position-action"
        or qualification.market_state != assessment.market_state
        or qualification.board != assessment.board
        or qualification.target != "D20/V60"
        or qualification.probability_grid != grid
        or qualification.user_id != scope.user_id
        or set(qualification.account_ids) != set(scope.account_ids)
        or qualification.account_type != "SIMULATED_CASH"
        or qualification.holding_age_domain != "INITIAL"
        or qualification.portfolio_scope != command.execution.portfolio_id
        or security.thesis is None
        or qualification.source != security.thesis.origin
        or any(u.origin != qualification.source for u in units)
        or record is None
        or record.decision_id != command.qualification_id
        or record.recorded_at > command.cutoff_at
        or record.evidence.available_at > command.cutoff_at
        or not qualification_is_current(record, command.cutoff_at)
        or record.status not in {"VALID", "AT_RISK"}
        or basis is None
        or basis.available_at > command.cutoff_at
        or basis.digest != digest
        or basis.market_calendar_version != assessment.calendar_version
    ):
        reasons.append("ACTION_QUALIFICATION_UNAVAILABLE")
    return tuple(reasons)
