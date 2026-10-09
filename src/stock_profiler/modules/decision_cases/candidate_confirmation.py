"""Host-owned batch confirmation, committed with all reservations in one event."""

from datetime import datetime, timedelta
from decimal import Context, localcontext
from typing import Any

from stock_profiler.modules.candidate_selection.current_eligibility import (
    candidate_qualification_eligibility,
)
from stock_profiler.modules.decision_cases.candidate_allocation import (
    adjudicate_candidate_allocation,
)
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.portfolio.allocation_contracts import (
    AllocationCommitment,
    CandidateAllocationOutcome,
)
from stock_profiler.modules.portfolio.confirmation_contracts import CandidateConfirmationOutcome
from stock_profiler.modules.portfolio.market_calendar import market_session_close_on


def adjudicate_candidate_confirmation(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    business_prerequisite_met: bool,
) -> CandidateConfirmationOutcome:
    command, scope = case.candidate_confirmation, case.access_scope
    assert command is not None and scope is not None
    source = ledger.get_formal_report_for_event(command.plan_event_id, connection)
    if (
        source is None
        or source.access_scope is None
        or not scope.same_scope_as(source.access_scope)
    ):
        return CandidateConfirmationOutcome(disposition="BLOCKED", reasons=("PLAN_UNAVAILABLE",))
    if not business_prerequisite_met:
        return CandidateConfirmationOutcome(
            disposition="BLOCKED", reasons=("BUSINESS_PREREQUISITE_NOT_MET",)
        )
    if (
        command.operation == "SUBMIT"
        and ledger.get_correction_event(command.plan_event_id, connection) is not None
    ):
        return CandidateConfirmationOutcome(disposition="BLOCKED", reasons=("PLAN_SUPERSEDED",))
    plan = source.result.candidate_allocation
    if plan is None or plan.disposition != "PLANNED" or plan.plan_id != command.plan_id:
        return CandidateConfirmationOutcome(
            disposition="BLOCKED", reasons=("PLAN_NOT_CONFIRMABLE",)
        )
    if (
        command.user_id != scope.user_id
        or scope.visibility != "USER"
        or plan.risk_handoff is None
        or command.portfolio_id != plan.risk_handoff.portfolio_id
        or command.portfolio_id != command.revalidation.risk_handoff.portfolio_id
        or command.candidate_batch_id != plan.candidate_batch_id
        or command.revalidation.candidate_event_id != plan.candidate_event_id
        or command.revalidation.policy != plan.policy
    ):
        return CandidateConfirmationOutcome(
            disposition="BLOCKED", reasons=("PLAN_IDENTITY_CONFLICT",)
        )
    now = datetime.fromisoformat(ledger.observed_at())
    if command.revalidation.cutoff_at > now:
        return CandidateConfirmationOutcome(
            disposition="BLOCKED", reasons=("REVALIDATION_AFTER_SUBMISSION",)
        )
    if command.operation == "SUBMIT" and not _entry_window_is_open(plan, now):
        return CandidateConfirmationOutcome(disposition="BLOCKED", reasons=("ENTRY_WINDOW_CLOSED",))
    if ledger.pending_candidate_confirmations(connection, scope, command.portfolio_id) - {
        case.business_object_id
    }:
        return CandidateConfirmationOutcome(
            disposition="BLOCKED", reasons=("CONFIRMATION_PENDING_RECONCILIATION",)
        )
    history = ledger.candidate_confirmation_history(connection, scope, command.portfolio_id)
    prior_fact = next(
        (
            fact
            for fact in reversed(history)
            if fact.result.candidate_confirmation is not None
            and fact.result.candidate_confirmation.plan_id == command.plan_id
        ),
        None,
    )
    prior = prior_fact.result.candidate_confirmation if prior_fact else None
    if command.seen_confirmation_id != (prior.confirmation_id if prior else None):
        return CandidateConfirmationOutcome(
            disposition="BLOCKED", reasons=("CONFIRMATION_VERSION_CONFLICT",)
        )
    choices = {row.security_id: row.choice for row in command.choices}
    required = {row.candidate.security_id for row in plan.rows if row.principal > 0}
    if len(choices) != len(command.choices) or set(choices) != required or not required:
        return CandidateConfirmationOutcome(
            disposition="BLOCKED", reasons=("COMPLETE_CHOICES_REQUIRED",)
        )
    prior_choices = {row.security_id: row.choice for row in prior.choices} if prior else {}
    if command.operation == "WITHDRAW" and (
        prior is None
        or any(
            choice == "ACCEPT" and prior_choices.get(security) != "ACCEPT"
            for security, choice in choices.items()
        )
    ):
        return CandidateConfirmationOutcome(
            disposition="BLOCKED", reasons=("WITHDRAWAL_VECTOR_INVALID",)
        )
    released = tuple(
        reservation
        for reservation in (prior.reservations if prior else ())
        if choices[reservation.security_id] != "ACCEPT"
    )
    if released:
        assert prior_fact is not None
        proof = (
            ledger.get_decision_event(command.withdrawal_position_event_id, connection)
            if command.withdrawal_position_event_id
            else None
        )
        if (
            proof is None
            or proof.corrects_event_id is not None
            or ledger.get_correction_event(proof.decision_event_id, connection) is not None
            or proof.case.access_scope is None
            or not scope.same_scope_as(proof.case.access_scope)
            or proof.result.position is None
            or proof.result.position.disposition != "RECONCILED"
            or not ledger.position_snapshot_is_latest(
                connection, scope, proof.result.position.snapshot
            )
            or not datetime.fromisoformat(prior_fact.committed_at)
            <= proof.result.position.snapshot.cutoff_at
            <= now
            or proof.result.position.snapshot.snapshot_evidence.problem_codes(
                now, require_current_completeness=True
            )
            or set(scope.account_ids)
            != {row.account_id for row in proof.result.position.snapshot.cash_states}
        ):
            return CandidateConfirmationOutcome(
                disposition="BLOCKED", reasons=("ORDER_EXCLUSION_REQUIRED",)
            )
        snapshot = proof.result.position.snapshot
        if any(
            order.account_id == reservation.account_id
            and order.security_id == reservation.security_id
            for reservation in released
            for order in snapshot.unfinished_orders
        ):
            return CandidateConfirmationOutcome(
                disposition="BLOCKED", reasons=("UNFINISHED_ORDER_PREVENTS_RELEASE",)
            )
        assert plan.position_snapshot is not None
        original_quantities = {
            (unit.account_id, unit.security_id): unit.total_quantity
            for unit in plan.position_snapshot.action_units
        }
        current_quantities = {
            (unit.account_id, unit.security_id): unit.total_quantity
            for unit in snapshot.action_units
        }
        if any(
            entry.account_id == reservation.account_id
            and entry.security_id == reservation.security_id
            and entry.entry_type == "FILL"
            and entry.quantity_delta > 0
            and entry.occurred_at >= datetime.fromisoformat(prior_fact.committed_at)
            for reservation in released
            for entry in snapshot.authoritative_ledger
        ) or any(
            current_quantities.get((reservation.account_id, reservation.security_id), 0) is None
            or original_quantities.get((reservation.account_id, reservation.security_id), 0) is None
            or current_quantities.get((reservation.account_id, reservation.security_id), 0)
            != original_quantities.get((reservation.account_id, reservation.security_id), 0)
            for reservation in released
        ):
            return CandidateConfirmationOutcome(
                disposition="BLOCKED", reasons=("RESERVATION_RECONCILIATION_REQUIRED",)
            )
    if command.operation == "SUBMIT":
        authorization_history = ledger.portfolio_authorization_history(
            connection,
            scope,
            command.portfolio_id,
            now.isoformat(),
        )
        if (
            not authorization_history
            or authorization_history[-1].authorization is None
            or authorization_history[-1].authorization.authorization_id
            != command.revalidation.risk_handoff.authorization_id
        ):
            return CandidateConfirmationOutcome(
                disposition="BLOCKED", reasons=("AUTHORIZATION_SUPERSEDED",)
            )
        candidate_fact = ledger.get_decision_event(
            command.revalidation.candidate_event_id, connection
        )
        if candidate_fact is None or candidate_fact.result.candidate_release is None:
            return CandidateConfirmationOutcome(
                disposition="BLOCKED", reasons=("CANDIDATE_HANDOFF_UNAVAILABLE",)
            )
        current_reasons, _, _ = candidate_qualification_eligibility(
            candidate_fact.result.candidate_release,
            candidate_fact.case,
            ledger.governance_history(connection, scope),
            now,
        )
        if current_reasons:
            return CandidateConfirmationOutcome(disposition="BLOCKED", reasons=current_reasons)
        current = adjudicate_candidate_allocation(
            case.model_copy(
                update={
                    "candidate_allocation": command.revalidation,
                }
            ),
            ledger,
            connection,
            business_prerequisite_met=business_prerequisite_met,
            exclude_confirmation_plan_id=command.plan_id,
        )
        if (
            current.disposition != "PLANNED"
            or current.position_snapshot is None
            or not ledger.position_snapshot_is_latest(connection, scope, current.position_snapshot)
            or _structured_rows(current) != _structured_rows(plan)
            or current.capacity_checks != plan.capacity_checks
            or current.purchase_sequence != plan.purchase_sequence
            or current.total_principal != plan.total_principal
            or current.remaining_cash != plan.remaining_cash
            or current.reasons != plan.reasons
            or current.risk_budget != plan.risk_budget
            or current.discrete_objectives != plan.discrete_objectives
            or current.correlations is None
            or plan.correlations is None
            or current.correlations.version_id != plan.correlations.version_id
            or current.correlations.market_calendar_version
            != plan.correlations.market_calendar_version
            or current.candidate_conclusion_version != plan.candidate_conclusion_version
            or any(
                leg.quantity <= 0
                or leg.route.price_cap is None
                or not leg.route.price_cap_confirmed
                or not leg.route.permission
                or leg.route.evidence.expires_at is not None
                and leg.route.evidence.expires_at < now
                for row in current.rows
                if choices.get(row.candidate.security_id) == "ACCEPT"
                for leg in row.legs
            )
            or plan.risk_budget is None
            or not plan.risk_budget.effective_at <= now < plan.risk_budget.expires_at
        ):
            return CandidateConfirmationOutcome(disposition="BLOCKED", reasons=("PLAN_CHANGED",))
    retained = {
        (row.security_id, row.account_id): row for row in (prior.reservations if prior else ())
    }
    with localcontext(Context(prec=38)):
        reservations = tuple(
            retained.get((row.candidate.security_id, leg.route.account_id))
            or AllocationCommitment(
                commitment_id=f"{case.decision_event_id}:{row.candidate.security_id}:{leg.route.account_id}",
                account_id=leg.route.account_id,
                security_id=row.candidate.security_id,
                issuer_id=row.issuer_id,
                broker_order_id=None,
                principal=leg.principal,
                quantity=leg.quantity,
                price_cap=leg.route.price_cap,
                purchase_cost=leg.purchase_cost,
                disposal_friction=leg.principal * leg.route.disposal_friction_ratio,
                evidence=leg.route.evidence,
            )
            for row in plan.rows
            if choices.get(row.candidate.security_id) == "ACCEPT"
            for leg in row.legs
        )
    return CandidateConfirmationOutcome(
        disposition="CONFIRMED",
        reasons=(),
        confirmation_id=case.decision_event_id,
        plan_id=plan.plan_id,
        portfolio_id=command.portfolio_id,
        choices=command.choices,
        reservations=reservations,
        supersedes_confirmation_id=prior.confirmation_id if prior else None,
        released_reservation_ids=tuple(row.commitment_id for row in released),
    )


def _structured_rows(plan: CandidateAllocationOutcome) -> tuple[dict[str, Any], ...]:
    """Compare execution facts while allowing independently revalidated provenance."""
    return tuple(
        row.model_dump(
            exclude={
                "legs": {"__all__": {"route": {"evidence"}}},
                "route_failures": {"__all__": {"route": {"evidence"}}},
            }
        )
        for row in plan.rows
    )


def finalize_candidate_confirmation(
    case: FrozenDecisionCase,
    outcome: CandidateConfirmationOutcome,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    committed_at: str,
) -> CandidateConfirmationOutcome:
    """Recheck time-sensitive gates after optimization without another long solve."""
    if outcome.disposition != "CONFIRMED":
        return outcome
    command, scope = case.candidate_confirmation, case.access_scope
    assert command is not None and scope is not None
    now = datetime.fromisoformat(committed_at)
    if outcome.released_reservation_ids:
        proof = (
            ledger.get_decision_event(command.withdrawal_position_event_id, connection)
            if command.withdrawal_position_event_id
            else None
        )
        if (
            proof is None
            or proof.result.position is None
            or not ledger.position_snapshot_is_latest(
                connection, scope, proof.result.position.snapshot
            )
            or proof.result.position.snapshot.snapshot_evidence.problem_codes(
                now, require_current_completeness=True
            )
        ):
            return CandidateConfirmationOutcome(
                disposition="BLOCKED", reasons=("ORDER_EXCLUSION_REQUIRED",)
            )
    if command.operation == "WITHDRAW":
        return outcome
    source = ledger.get_formal_report_for_event(command.plan_event_id, connection)
    assert source is not None and source.result.candidate_allocation is not None
    plan = source.result.candidate_allocation
    assert plan.risk_budget is not None
    if not _entry_window_is_open(plan, now):
        return CandidateConfirmationOutcome(disposition="BLOCKED", reasons=("ENTRY_WINDOW_CLOSED",))
    if not plan.risk_budget.effective_at <= now < plan.risk_budget.expires_at:
        return CandidateConfirmationOutcome(disposition="BLOCKED", reasons=("RISK_BUDGET_EXPIRED",))
    candidate = ledger.get_decision_event(command.revalidation.candidate_event_id, connection)
    assert candidate is not None and candidate.result.candidate_release is not None
    reasons, _, _ = candidate_qualification_eligibility(
        candidate.result.candidate_release,
        candidate.case,
        ledger.governance_history(connection, scope),
        now,
    )
    if reasons:
        return CandidateConfirmationOutcome(disposition="BLOCKED", reasons=reasons)
    current_evidence = [command.revalidation.model_dump(mode="python")]
    for event_id in (
        command.revalidation.risk_handoff.concentration_event_id,
        command.revalidation.risk_handoff.stress_event_id,
        command.revalidation.risk_handoff.liquidity_event_id,
        command.revalidation.risk_handoff.drawdown_event_id,
    ):
        fact = ledger.get_decision_event(event_id, connection)
        if fact is None:
            return CandidateConfirmationOutcome(
                disposition="BLOCKED", reasons=("RISK_HANDOFF_UNAVAILABLE",)
            )
        current_evidence.append(fact.case.model_dump(mode="python"))
    if any(
        _current_evidence_expired(value, command.revalidation.cutoff_at, now)
        for value in current_evidence
    ):
        return CandidateConfirmationOutcome(
            disposition="BLOCKED", reasons=("CURRENT_EVIDENCE_EXPIRED",)
        )
    return outcome


def _current_evidence_expired(value: Any, cutoff: datetime, now: datetime) -> bool:
    if isinstance(value, dict):
        if (
            value.get("cutoff_at") == cutoff
            and isinstance(value.get("expires_at"), datetime)
            and value["expires_at"] < now
        ):
            return True
        return any(_current_evidence_expired(item, cutoff, now) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_current_evidence_expired(item, cutoff, now) for item in value)
    return False


def _entry_window_is_open(plan: CandidateAllocationOutcome, now: datetime) -> bool:
    if plan.correlations is None:
        return False
    for row in plan.rows:
        dates = row.candidate.valid_market_dates
        start = (
            market_session_close_on(dates[0], plan.correlations.market_calendar_version)
            if dates
            else None
        )
        end = (
            market_session_close_on(dates[-1], plan.correlations.market_calendar_version)
            if dates
            else None
        )
        if start is None or end is None or not start - timedelta(hours=7) <= now <= end:
            return False
    return True
