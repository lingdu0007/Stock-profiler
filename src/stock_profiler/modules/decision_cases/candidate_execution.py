"""Append execution declarations without changing authoritative broker facts."""

from datetime import datetime
from decimal import Context, Decimal, DecimalException, localcontext

from stock_profiler.modules.decision_cases.candidate_confirmation import (
    _current_evidence_expired,
    _entry_window_is_open,
)
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.portfolio.allocation_contracts import AllocationCommitment
from stock_profiler.modules.portfolio.confirmation_contracts import CandidateConfirmationOutcome
from stock_profiler.modules.portfolio.execution_contracts import (
    AttributedFill,
    AttributedOrder,
    BrokerExecutionOrder,
    CandidateExecutionCommand,
    CandidateExecutionOutcome,
    ExecutionClassification,
    ExecutionRow,
)
from stock_profiler.modules.position_management.contracts import (
    ReconciledPositionSnapshot,
    ledger_entry_evidence_is_visible,
)


def adjudicate_candidate_execution(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    business_prerequisite_met: bool,
) -> CandidateExecutionOutcome:
    command, scope = case.candidate_execution, case.access_scope
    assert command is not None and scope is not None
    plan = ledger.get_formal_report_for_event(command.plan_event_id, connection)
    confirmed = ledger.get_decision_event(command.confirmation_event_id, connection)
    if (
        not business_prerequisite_met
        or scope.visibility != "USER"
        or plan is None
        or plan.access_scope is None
        or not scope.same_scope_as(plan.access_scope)
        or plan.result.candidate_allocation is None
        or confirmed is None
        or confirmed.case.access_scope is None
        or not scope.same_scope_as(confirmed.case.access_scope)
        or confirmed.result.candidate_confirmation is None
        or confirmed.result.candidate_confirmation.disposition != "CONFIRMED"
        or confirmed.result.candidate_confirmation.plan_id
        != plan.result.candidate_allocation.plan_id
        or confirmed.result.candidate_confirmation.portfolio_id != command.portfolio_id
    ):
        return CandidateExecutionOutcome(
            disposition="BLOCKED", reasons=("EXECUTION_SOURCE_UNAVAILABLE",)
        )
    confirmation = confirmed.result.candidate_confirmation
    history = ledger.candidate_execution_history(connection, scope, command.portfolio_id)
    prior = next(
        (
            fact.result.candidate_execution
            for fact in reversed(history)
            if fact.result.candidate_execution is not None
            and fact.case.candidate_execution is not None
            and fact.case.candidate_execution.confirmation_event_id == command.confirmation_event_id
        ),
        None,
    )
    base = prior or CandidateExecutionOutcome(
        disposition="PENDING_RECONCILIATION",
        reasons=(),
        portfolio_id=command.portfolio_id,
        plan_id=confirmation.plan_id,
        confirmation_id=confirmation.confirmation_id,
        reservations=confirmation.reservations,
    )

    def blocked(reason: str) -> CandidateExecutionOutcome:
        return base.model_copy(
            update={
                "disposition": "BLOCKED",
                "reasons": (reason,),
                "execution_id": case.decision_event_id,
                "terminal_outcome": None,
            }
        )

    if command.seen_execution_id != (prior.execution_id if prior else None):
        return blocked("EXECUTION_VERSION_CONFLICT")
    if ledger.pending_candidate_executions(connection, scope, command.portfolio_id) - {
        case.business_object_id
    }:
        return blocked("EXECUTION_PENDING_RECONCILIATION")
    latest_confirmation = next(
        (
            fact
            for fact in reversed(
                ledger.candidate_confirmation_history(connection, scope, command.portfolio_id)
            )
            if fact.result.candidate_confirmation is not None
            and fact.result.candidate_confirmation.plan_id == confirmation.plan_id
        ),
        None,
    )
    if (
        latest_confirmation is None
        or latest_confirmation.decision_event_id != command.confirmation_event_id
    ):
        return blocked("CONFIRMATION_SUPERSEDED")
    if command.operation == "RECONCILE":
        try:
            with localcontext(Context(prec=38)):
                outcome = _reconcile(
                    case,
                    ledger,
                    connection,
                    confirmation,
                    previously_withdrawn=base.withdrawn_reservation_ids,
                )
        except DecimalException:
            return blocked("EXECUTION_ARITHMETIC_UNAVAILABLE")
        if outcome.disposition == "BLOCKED":
            unresolved = set(base.unresolved_order_keys)
            proof = (
                ledger.get_decision_event(command.position_event_id, connection)
                if command.position_event_id
                else None
            )
            if (
                proof is not None
                and proof.case.access_scope is not None
                and scope.same_scope_as(proof.case.access_scope)
                and proof.result.position is not None
                and proof.result.position.disposition == "RECONCILED"
            ):
                unresolved.update(
                    (order.account_id, order.order_id)
                    for order in proof.result.position.snapshot.unfinished_orders
                    if order.side == "BUY"
                )
            return base.model_copy(
                update={
                    "disposition": "BLOCKED",
                    "reasons": outcome.reasons,
                    "execution_id": case.decision_event_id,
                    "terminal_outcome": None,
                    "unresolved_order_keys": tuple(sorted(unresolved)),
                }
            )
        for fact in ledger.candidate_execution_history(connection, scope):
            previous = fact.result.candidate_execution
            if previous is None:
                continue
            for collection, key_field in (
                ("fills", "entry_id"),
                ("order_attributions", "order_id"),
            ):
                owned = {
                    (row.account_id, getattr(row, key_field)): row.reservation_id
                    for row in getattr(previous, collection)
                    if row.reservation_id is not None
                }
                if any(
                    (row.account_id, getattr(row, key_field)) in owned
                    and owned[(row.account_id, getattr(row, key_field))] != row.reservation_id
                    for row in getattr(outcome, collection)
                ):
                    return blocked("BROKER_ATTRIBUTION_CONFLICT")
        return outcome.model_copy(update={"declarations": base.declarations})
    if command.declaration is None:
        return blocked("DECLARATION_REQUIRED")
    if command.declaration.security_id not in {
        row.security_id for row in confirmation.reservations
    } or not datetime.fromisoformat(
        confirmed.committed_at
    ) <= command.declaration.declared_at <= command.cutoff_at <= datetime.fromisoformat(
        ledger.observed_at()
    ):
        return blocked("DECLARATION_INVALID")
    return base.model_copy(
        update={
            "disposition": "PENDING_RECONCILIATION",
            "reasons": base.reasons,
            "execution_id": case.decision_event_id,
            "declarations": (*base.declarations, command.declaration),
        }
    )


def _reconcile(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    confirmation: CandidateConfirmationOutcome,
    *,
    previously_withdrawn: tuple[str, ...],
) -> CandidateExecutionOutcome:
    command, scope = case.candidate_execution, case.access_scope
    assert command is not None and scope is not None
    reservations = confirmation.reservations
    confirmation_history = ledger.candidate_confirmation_history(
        connection, scope, command.portfolio_id
    )
    global_reservation_map: dict[str, AllocationCommitment] = {}
    for fact in confirmation_history:
        assert fact.result.candidate_confirmation is not None
        for confirmed_reservation in fact.result.candidate_confirmation.reservations:
            global_reservation_map.setdefault(
                confirmed_reservation.commitment_id, confirmed_reservation
            )
    global_reservations = tuple(global_reservation_map.values())
    history_start_at = min(
        datetime.fromisoformat(fact.committed_at) for fact in confirmation_history
    )
    proof = (
        ledger.get_decision_event(command.position_event_id, connection)
        if command.position_event_id
        else None
    )
    if (
        proof is None
        or proof.case.access_scope is None
        or not scope.same_scope_as(proof.case.access_scope)
        or proof.result.position is None
        or proof.result.position.disposition != "RECONCILED"
        or proof.corrects_event_id is not None
        or ledger.get_correction_event(proof.decision_event_id, connection) is not None
        or command.cutoff_at > datetime.fromisoformat(ledger.observed_at())
        or proof.result.position.snapshot.cutoff_at != command.cutoff_at
        or not ledger.position_snapshot_is_latest(connection, scope, proof.result.position.snapshot)
        or command.broker_evidence is None
        or command.broker_evidence.problem_codes(
            command.cutoff_at, require_current_completeness=True
        )
    ):
        return CandidateExecutionOutcome(
            disposition="BLOCKED", reasons=("BROKER_FACTS_INCOMPLETE",)
        )
    snapshot = proof.result.position.snapshot
    plan_report = ledger.get_formal_report_for_event(command.plan_event_id, connection)
    assert plan_report is not None and plan_report.result.candidate_allocation is not None
    plan = plan_report.result.candidate_allocation
    expired = not _entry_window_is_open(plan, command.cutoff_at)
    invalidated = ledger.get_correction_event(command.plan_event_id, connection) is not None
    withdrawn = set(command.withdrawn_reservation_ids) | set(previously_withdrawn)
    if len(set(command.withdrawn_reservation_ids)) != len(
        command.withdrawn_reservation_ids
    ) or not withdrawn <= {r.commitment_id for r in reservations}:
        return CandidateExecutionOutcome(
            disposition="BLOCKED", reasons=("WITHDRAWAL_IDENTITY_INVALID",)
        )
    confirmed = ledger.get_decision_event(command.confirmation_event_id, connection)
    assert confirmed is not None
    broker_problems = _broker_problems(
        command,
        snapshot,
        scope.account_ids,
        datetime.fromisoformat(confirmed.committed_at),
        history_start_at=history_start_at,
    )
    if broker_problems:
        return CandidateExecutionOutcome(disposition="BLOCKED", reasons=broker_problems)
    entries = {(row.account_id, row.entry_id): row for row in snapshot.authoritative_ledger}
    orders = {(row.account_id, row.order_id): row for row in command.orders}
    latest = {}
    for fact in ledger.candidate_execution_history(connection, scope, command.portfolio_id):
        if (
            fact.case.candidate_execution is not None
            and fact.result.candidate_execution is not None
        ):
            latest[fact.case.candidate_execution.confirmation_event_id] = (
                fact.result.candidate_execution
            )
    pending = {key for prior in latest.values() for key in prior.unresolved_order_keys} | {
        (order.account_id, order.order_id)
        for prior in latest.values()
        for order in prior.orders
        if order.status in {"OPEN", "UNKNOWN"}
    }
    if not pending <= set(orders):
        return CandidateExecutionOutcome(
            disposition="BLOCKED", reasons=("BROKER_ORDER_TERMINAL_UNPROVEN",)
        )
    reservation_map = global_reservation_map
    order_attributions = {}
    for broker_order in command.orders:
        reservation = reservation_map.get(broker_order.causal_reservation_id or "")
        classification, reasons = _classify_order(
            case, ledger, connection, broker_order, reservation, global_reservations
        )
        order_attributions[(broker_order.account_id, broker_order.order_id)] = AttributedOrder(
            account_id=broker_order.account_id,
            order_id=broker_order.order_id,
            reservation_id=reservation.commitment_id
            if reservation is not None and classification in {"PLANNED", "DEVIATION"}
            else None,
            classification=classification,
            reasons=reasons,
        )
    if any(
        order.status in {"OPEN", "UNKNOWN"}
        and (
            order_attributions[(order.account_id, order.order_id)].reservation_id in withdrawn
            or order_attributions[(order.account_id, order.order_id)].classification == "UNKNOWN"
            and any(
                r.commitment_id in withdrawn and r.security_id == order.security_id
                for r in reservations
            )
        )
        for order in command.orders
    ):
        return CandidateExecutionOutcome(
            disposition="BLOCKED", reasons=("UNFINISHED_ORDER_PREVENTS_RELEASE",)
        )
    attributed: list[AttributedFill] = []
    with localcontext(Context(prec=38)):
        for fill in command.fills:
            entry = entries.get((fill.account_id, fill.entry_id))
            order = orders.get((fill.account_id, fill.order_id))
            if (
                entry is None
                or order is None
                or entry.entry_type != "FILL"
                or entry.quantity_delta <= 0
                and entry.corrects_entry_id is None
                or not ledger_entry_evidence_is_visible(entry, command.cutoff_at)
                or entry.security_id != order.security_id
                or (
                    entry.quantity_delta != 0
                    and -entry.cash_delta != entry.quantity_delta * fill.price
                )
            ):
                return CandidateExecutionOutcome(
                    disposition="BLOCKED", reasons=("FILL_FACT_CONFLICT",)
                )
            if entry.corrects_entry_id is not None:
                target = entries.get((fill.account_id, entry.corrects_entry_id))
                target_fill = next(
                    (
                        row
                        for row in command.fills
                        if row.account_id == fill.account_id
                        and row.entry_id == entry.corrects_entry_id
                    ),
                    None,
                )
                if (
                    target is None
                    or target_fill is None
                    or target_fill.order_id != fill.order_id
                    or target.security_id != entry.security_id
                    or target.entry_type != "FILL"
                ):
                    return CandidateExecutionOutcome(
                        disposition="BLOCKED", reasons=("FILL_CORRECTION_IDENTITY_CONFLICT",)
                    )
            fee_entries = [
                entries.get((fill.account_id, identity)) for identity in fill.fee_entry_ids
            ]
            if any(
                fee is None
                or fee.entry_type != "FEE"
                or fee.security_id not in {None, order.security_id}
                or not ledger_entry_evidence_is_visible(fee, command.cutoff_at)
                for fee in fee_entries
            ):
                return CandidateExecutionOutcome(
                    disposition="BLOCKED", reasons=("FEE_FACT_CONFLICT",)
                )
            fees = sum((-fee.cash_delta for fee in fee_entries if fee is not None), Decimal(0))
            reservation = reservation_map.get(order.causal_reservation_id or "")
            attribution = order_attributions[(order.account_id, order.order_id)]
            classification, deviations = attribution.classification, attribution.reasons
            if (
                reservation is not None
                and classification in {"PLANNED", "DEVIATION"}
                and fill.price > (reservation.price_cap or Decimal(0))
            ):
                classification = "DEVIATION"
                deviations = (*deviations, "FILL_PRICE_DEVIATION")
            attributed.append(
                AttributedFill(
                    entry_id=fill.entry_id,
                    order_id=fill.order_id,
                    account_id=fill.account_id,
                    security_id=order.security_id,
                    reservation_id=reservation.commitment_id
                    if reservation and classification in {"PLANNED", "DEVIATION"}
                    else None,
                    classification=classification,
                    reasons=deviations,
                    quantity=entry.quantity_delta,
                    cash_used=-entry.cash_delta + fees,
                    fees=fees,
                    corrects_entry_id=entry.corrects_entry_id,
                )
            )
        for reservation in global_reservations:
            assert reservation.quantity is not None
            cumulative = Decimal(0)
            accepted = Decimal(0)
            indices = [
                index
                for index, row in enumerate(attributed)
                if row.reservation_id == reservation.commitment_id
            ]
            indices.sort(
                key=lambda index: (
                    entries[(attributed[index].account_id, attributed[index].entry_id)].occurred_at,
                    attributed[index].entry_id,
                )
            )
            for index in indices:
                row = attributed[index]
                cumulative += row.quantity
                if cumulative < 0:
                    return CandidateExecutionOutcome(
                        disposition="BLOCKED", reasons=("FILL_CORRECTION_QUANTITY_CONFLICT",)
                    )
                now_accepted = min(cumulative, reservation.quantity)
                matched = now_accepted - accepted
                attributed[index] = row.model_copy(
                    update={"intent_quantity": matched, "external_quantity": row.quantity - matched}
                )
                accepted = now_accepted
        attributed = [
            row.model_copy(update={"external_quantity": row.quantity})
            if row.classification == "EXTERNAL"
            else row
            for row in attributed
        ]
        remaining_claims: list[AllocationCommitment] = []
        rows: list[ExecutionRow] = []
        released: list[str] = []
        cost_problems = []
        for reservation in reservations:
            filled = sum(
                (
                    row.intent_quantity
                    for row in attributed
                    if row.reservation_id == reservation.commitment_id
                ),
                Decimal(0),
            )
            assert reservation.quantity is not None and reservation.price_cap is not None
            remaining = max(Decimal(0), reservation.quantity - filled)
            related = [
                order
                for order in command.orders
                if order_attributions[(order.account_id, order.order_id)].reservation_id
                == reservation.commitment_id
            ]
            unknown = any(
                order.security_id == reservation.security_id
                and order_attributions[(order.account_id, order.order_id)].classification
                == "UNKNOWN"
                for order in command.orders
            )
            final_orders = not any(order.status in {"OPEN", "UNKNOWN"} for order in related)
            may_release = (
                not unknown
                and final_orders
                and (
                    not remaining
                    or reservation.commitment_id in withdrawn
                    or expired
                    or invalidated
                    or bool(related)
                    and not filled
                )
            )
            rows.append(
                ExecutionRow(
                    reservation_id=reservation.commitment_id,
                    security_id=reservation.security_id,
                    accepted_quantity=reservation.quantity,
                    filled_quantity=filled,
                    remaining_quantity=remaining,
                    state="UNKNOWN"
                    if unknown
                    else "TERMINAL_UNFILLED"
                    if may_release and remaining
                    else "PARTIALLY_FILLED"
                    if filled and remaining
                    else "FILLED"
                    if filled
                    else "UNEXECUTED",
                )
            )
            linked = [
                order
                for order in command.orders
                if order_attributions[(order.account_id, order.order_id)].reservation_id
                == reservation.commitment_id
                and order.side == "BUY"
                and order.status in {"OPEN", "UNKNOWN"}
            ]
            broker_principal = sum(
                (order.remaining_quantity or order.quantity)
                * (order.limit_price or reservation.price_cap)
                for order in linked
            )
            claim_quantity = max(
                Decimal(0) if may_release else remaining,
                sum((order.remaining_quantity or order.quantity) for order in linked),
            )
            claim_principal = max(
                (Decimal(0) if may_release else remaining) * reservation.price_cap, broker_principal
            )
            funds_known = (
                command.funds_evidence is not None
                and not command.funds_evidence.problem_codes(
                    command.cutoff_at, require_current_completeness=True
                )
            )
            saved = sum(
                (
                    row.cash_used * row.intent_quantity / row.quantity
                    if row.quantity
                    else row.cash_used
                    for row in attributed
                    if row.reservation_id == reservation.commitment_id
                ),
                Decimal(0),
            )
            savings_hold = (
                max(Decimal(0), filled * reservation.price_cap - saved)
                if not funds_known
                else Decimal(0)
            )
            broker_cash = sum((order.reserved_cash or Decimal(0)) for order in linked)
            purchase_cost = reservation.purchase_cost
            disposal_friction = (
                claim_principal * reservation.disposal_friction / reservation.principal
            )
            deviated_account = any(order.account_id != reservation.account_id for order in linked)
            if linked:
                actual_cost = Decimal(0)
                actual_friction = Decimal(0)
                for order in linked:
                    route = next(
                        (
                            r
                            for r in plan.routes
                            if r.account_id == order.account_id
                            and r.security_id == order.security_id
                        ),
                        None,
                    )
                    if route is None:
                        cost_problems.append("LINKED_ORDER_COSTS_UNAVAILABLE")
                        continue
                    principal = (order.remaining_quantity or order.quantity) * (
                        order.limit_price or reservation.price_cap
                    )
                    actual_cost += route.cost(principal)
                    actual_friction += principal * route.disposal_friction_ratio
                purchase_cost = max(purchase_cost, actual_cost)
                disposal_friction = max(disposal_friction, actual_friction)
            cash_hold = max(
                savings_hold
                + (
                    reservation.purchase_cost
                    if not claim_quantity and not funds_known
                    else Decimal(0)
                ),
                broker_cash - claim_principal - purchase_cost,
            )
            if may_release and not claim_quantity and not cash_hold:
                released.append(reservation.commitment_id)
            if claim_quantity or cash_hold:
                remaining_claims.append(
                    reservation.model_copy(
                        update={
                            "quantity": claim_quantity or None,
                            "price_cap": claim_principal / claim_quantity
                            if claim_quantity
                            else None,
                            "principal": claim_principal,
                            "purchase_cost": purchase_cost if claim_quantity else Decimal(0),
                            "broker_order_id": linked[0].order_id
                            if len(linked) == 1 and not deviated_account
                            else None,
                            "broker_order_bindings": tuple(
                                (order.account_id, order.order_id) for order in linked
                            )
                            if deviated_account or len(linked) > 1
                            else (),
                            "disposal_friction": disposal_friction,
                            "reconciliation_cash_hold": cash_hold,
                        }
                    )
                )
        unassociated = []
        for order in command.orders:
            if (
                order.status not in {"OPEN", "UNKNOWN"}
                or order_attributions[(order.account_id, order.order_id)].reservation_id is not None
            ):
                continue
            principal = (order.remaining_quantity or Decimal(0)) * (order.limit_price or Decimal(0))
            route = next(
                (
                    r
                    for r in plan.routes
                    if r.account_id == order.account_id and r.security_id == order.security_id
                ),
                None,
            )
            if route is None:
                cost_problems.append("EXTERNAL_ORDER_COSTS_UNAVAILABLE")
            cost = route.cost(principal) if route else Decimal(0)
            unassociated.append(
                AllocationCommitment(
                    commitment_id=f"broker-order:{order.account_id}:{order.order_id}",
                    account_id=order.account_id,
                    security_id=order.security_id,
                    issuer_id=order.issuer_id,
                    broker_order_id=order.order_id,
                    quantity=order.remaining_quantity,
                    price_cap=order.limit_price,
                    principal=principal,
                    purchase_cost=cost,
                    reconciliation_cash_hold=max(
                        Decimal(0), (order.reserved_cash or Decimal(0)) - principal - cost
                    ),
                    disposal_friction=principal * route.disposal_friction_ratio
                    if route
                    else Decimal(0),
                    evidence=order.evidence,
                )
            )
        return CandidateExecutionOutcome(
            disposition="RECONCILED",
            reasons=tuple(dict.fromkeys(cost_problems)),
            execution_id=case.decision_event_id,
            portfolio_id=command.portfolio_id,
            plan_id=confirmation.plan_id,
            confirmation_id=command.confirmation_event_id,
            position_event_id=command.position_event_id,
            reservations=tuple(remaining_claims),
            unassociated_commitments=tuple(unassociated),
            orders=command.orders,
            order_attributions=tuple(order_attributions.values()),
            fills=tuple(attributed),
            rows=tuple(rows),
            released_reservation_ids=tuple(released),
            withdrawn_reservation_ids=tuple(sorted(withdrawn)),
            terminal_outcome=(
                "FULLY_FILLED"
                if rows and all(row.filled_quantity == row.accepted_quantity for row in rows)
                else "PARTIALLY_FILLED"
                if any(row.filled_quantity for row in rows)
                else "ALL_DECLINED"
                if confirmation.choices
                and all(row.choice == "DECLINE" for row in confirmation.choices)
                else "DEFERRED_EXPIRED"
                if any(row.choice == "DEFER" for row in confirmation.choices)
                else "NO_TRADE"
            )
            if (
                not remaining_claims
                and not any(
                    order.status in {"OPEN", "UNKNOWN"}
                    and order_attributions[(order.account_id, order.order_id)].reservation_id
                    in {r.commitment_id for r in reservations}
                    for order in command.orders
                )
                and not any(row.classification == "UNKNOWN" for row in order_attributions.values())
                and command.funds_evidence is not None
                and not command.funds_evidence.problem_codes(
                    command.cutoff_at, require_current_completeness=True
                )
                and (expired or not any(row.choice == "DEFER" for row in confirmation.choices))
            )
            else None,
        )


def _classify_order(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    order: BrokerExecutionOrder,
    reservation: AllocationCommitment | None,
    reservations: tuple[AllocationCommitment, ...],
) -> tuple[ExecutionClassification, tuple[str, ...]]:
    assert case.candidate_execution is not None and case.access_scope is not None
    if reservation is None:
        if order.external_origin_id and order.causal_reservation_id is None:
            return "EXTERNAL", ()
        plausible = any(row.security_id == order.security_id for row in reservations)
        return (
            ("UNKNOWN", ("CAUSALITY_UNPROVEN",))
            if plausible or order.causal_reservation_id
            else ("EXTERNAL", ())
        )
    if (
        order.security_id != reservation.security_id
        or order.side != "BUY"
        or order.issuer_id != reservation.issuer_id
    ):
        return "UNKNOWN", ("CAUSAL_IDENTITY_CONFLICT",)
    origin = next(
        (
            fact
            for fact in ledger.candidate_confirmation_history(
                connection, case.access_scope, case.candidate_execution.portfolio_id
            )
            if fact.result.candidate_confirmation is not None
            and any(
                r.commitment_id == reservation.commitment_id
                for r in fact.result.candidate_confirmation.reservations
            )
        ),
        None,
    )
    if (
        origin is None
        or origin.case.access_scope is None
        or not case.access_scope.same_scope_as(origin.case.access_scope)
        or origin.case.candidate_confirmation is None
    ):
        return "UNKNOWN", ("CAUSAL_SOURCE_UNAVAILABLE",)
    if order.submitted_at < datetime.fromisoformat(origin.committed_at):
        return "UNKNOWN", ("CAUSAL_TIME_CONFLICT",)
    assert origin.result.candidate_confirmation is not None
    deviations = []
    plan = ledger.get_formal_report_for_event(
        origin.case.candidate_confirmation.plan_event_id, connection
    )
    if plan is None or plan.result.candidate_allocation is None:
        return "UNKNOWN", ("CAUSAL_SOURCE_UNAVAILABLE",)
    if not _entry_window_is_open(plan.result.candidate_allocation, order.submitted_at):
        deviations.append("ENTRY_WINDOW_DEVIATION")
    if order.account_id != reservation.account_id:
        deviations.append("ACCOUNT_DEVIATION")
    command = case.candidate_execution
    earlier = [
        row
        for row in command.orders
        if row.causal_reservation_id == reservation.commitment_id
        and (row.submitted_at, row.account_id, row.order_id)
        < (order.submitted_at, order.account_id, order.order_id)
    ]
    consumed = sum(
        (
            row.filled_quantity + (row.remaining_quantity or Decimal(0))
            if row.status == "OPEN"
            else row.filled_quantity
            for row in earlier
        ),
        Decimal(0),
    )
    if order.quantity > max(Decimal(0), (reservation.quantity or Decimal(0)) - consumed):
        deviations.append("QUANTITY_DEVIATION")
    if order.limit_price is None or order.limit_price > (reservation.price_cap or Decimal(0)):
        deviations.append("PRICE_DEVIATION")
    step = (
        ledger.get_decision_event(order.step_confirmation_id, connection)
        if order.step_confirmation_id
        else None
    )
    if (
        step is None
        or step.case.access_scope is None
        or not case.access_scope.same_scope_as(step.case.access_scope)
        or step.result.candidate_confirmation is None
        or step.result.candidate_confirmation.disposition != "CONFIRMED"
        or step.decision_event_id == origin.decision_event_id
        or step.case.candidate_confirmation is None
        or step.case.candidate_confirmation.operation != "SUBMIT"
        or not any(
            row.commitment_id == reservation.commitment_id
            for row in step.result.candidate_confirmation.reservations
        )
        or datetime.fromisoformat(step.committed_at) > order.submitted_at
        or any(datetime.fromisoformat(step.committed_at) <= row.submitted_at for row in earlier)
        or _current_evidence_expired(
            step.case.candidate_confirmation.model_dump(mode="python"),
            step.case.candidate_confirmation.revalidation.cutoff_at,
            order.submitted_at,
        )
    ):
        deviations.append("STEP_REVIEW_UNPROVEN")
    proof = ledger.get_decision_event(command.position_event_id or "", connection)
    assert proof is not None and proof.result.position is not None
    entries = {
        (row.account_id, row.entry_id): row
        for row in proof.result.position.snapshot.authoritative_ledger
    }
    previous_fills = [
        entries[(row.account_id, row.entry_id)]
        for row in command.fills
        if entries[(row.account_id, row.entry_id)].occurred_at < order.submitted_at
    ]
    if (
        step is not None
        and step.case.candidate_confirmation is not None
        and any(
            row.occurred_at > step.case.candidate_confirmation.revalidation.cutoff_at
            for row in previous_fills
        )
    ):
        deviations.append("STEP_REVIEW_UNPROVEN")
    accepted = {
        row.security_id
        for row in origin.result.candidate_confirmation.choices
        if row.choice == "ACCEPT"
    }
    sequence = [
        security
        for security in plan.result.candidate_allocation.purchase_sequence
        if security in accepted
    ]
    before = (
        sequence[: sequence.index(order.security_id)] if order.security_id in sequence else sequence
    )
    for security in before:
        intended = [
            row
            for row in reservations
            if row.security_id == security
            and any(
                row.commitment_id == r.commitment_id
                for r in origin.result.candidate_confirmation.reservations
            )
        ]
        required = sum((row.quantity or Decimal(0) for row in intended), Decimal(0))
        intended_ids = {row.commitment_id for row in intended}
        order_ids = {
            (row.account_id, row.order_id)
            for row in command.orders
            if row.causal_reservation_id in intended_ids
            and row.security_id == security
            and row.submitted_at >= datetime.fromisoformat(origin.committed_at)
        }
        filled_before = sum(
            (
                entries[(fill.account_id, fill.entry_id)].quantity_delta
                for fill in command.fills
                if (fill.account_id, fill.order_id) in order_ids
                and entries[(fill.account_id, fill.entry_id)].occurred_at < order.submitted_at
            ),
            Decimal(0),
        )
        if required == 0 or filled_before < required:
            deviations.append("SEQUENCE_DEVIATION")
            break
    return ("DEVIATION" if deviations else "PLANNED"), tuple(dict.fromkeys(deviations))


def _broker_problems(
    command: CandidateExecutionCommand,
    snapshot: ReconciledPositionSnapshot,
    account_ids: tuple[str, ...],
    confirmed_at: datetime,
    *,
    history_start_at: datetime,
) -> tuple[str, ...]:
    orders = {(row.account_id, row.order_id): row for row in command.orders}
    fills = {(row.account_id, row.entry_id): row for row in command.fills}
    if len(orders) != len(command.orders) or len(fills) != len(command.fills):
        return ("DUPLICATE_BROKER_IDENTITY",)
    if (
        set(account_ids) != {row.account_id for row in snapshot.cash_states}
        or command.cutoff_at < confirmed_at
    ):
        return ("BROKER_SCOPE_OR_TIME_CONFLICT",)
    if any(row.external_origin_id and row.causal_reservation_id for row in command.orders):
        return ("BROKER_ORIGIN_CONFLICT",)
    if any(row.account_id not in account_ids for row in command.orders):
        return ("BROKER_ACCOUNT_OUT_OF_SCOPE",)
    required = {
        (row.account_id, row.entry_id)
        for row in snapshot.authoritative_ledger
        if row.entry_type == "FILL"
        and (row.quantity_delta > 0 or row.corrects_entry_id is not None)
        and row.occurred_at >= history_start_at
    }
    if set(fills) != required:
        return ("BROKER_FILLS_INCOMPLETE",)
    required_fees = {
        (row.account_id, row.entry_id)
        for row in snapshot.authoritative_ledger
        if row.entry_type == "FEE" and row.occurred_at >= history_start_at
    }
    supplied_fees = [
        (fill.account_id, identity) for fill in command.fills for identity in fill.fee_entry_ids
    ]
    if len(set(supplied_fees)) != len(supplied_fees) or set(supplied_fees) != required_fees:
        return ("BROKER_FEES_INCOMPLETE_OR_DUPLICATED",)
    open_orders = {
        (row.account_id, row.order_id): row
        for row in snapshot.unfinished_orders
        if row.side == "BUY"
    }
    if set(open_orders) != {
        key
        for key, row in orders.items()
        if row.side == "BUY" and row.status in {"OPEN", "UNKNOWN"}
    }:
        return ("BROKER_ORDERS_INCOMPLETE",)
    entries = {(row.account_id, row.entry_id): row for row in snapshot.authoritative_ledger}
    for order in command.orders:
        if (
            order.side != "BUY"
            or order.remaining_quantity is None
            or order.limit_price is None
            or order.reserved_cash is None
            or order.status == "UNKNOWN"
        ):
            return ("BROKER_ORDER_UNKNOWN",)
        if (
            order.evidence.problem_codes(command.cutoff_at, require_current_completeness=True)
            or order.submitted_at > command.cutoff_at
        ):
            return ("BROKER_ORDER_EVIDENCE_FAILED",)
        total = sum(
            (
                entries[(fill.account_id, fill.entry_id)].quantity_delta
                for fill in command.fills
                if fill.account_id == order.account_id and fill.order_id == order.order_id
            ),
            Decimal(0),
        )
        if total != order.filled_quantity or order.filled_quantity > order.quantity:
            return ("BROKER_ORDER_QUANTITY_CONFLICT",)
        if (
            order.status == "OPEN"
            and order.remaining_quantity != order.quantity - order.filled_quantity
        ):
            return ("BROKER_ORDER_QUANTITY_CONFLICT",)
        if order.status not in {"OPEN", "UNKNOWN"} and (
            order.remaining_quantity != 0 or order.reserved_cash != 0
        ):
            return ("BROKER_TERMINAL_CONFLICT",)
        if order.status == "FILLED" and order.filled_quantity != order.quantity:
            return ("BROKER_TERMINAL_CONFLICT",)
        opened = open_orders.get((order.account_id, order.order_id))
        if opened is not None and (
            opened.security_id != order.security_id
            or opened.remaining_quantity != order.remaining_quantity
            or opened.reserved_cash != order.reserved_cash
        ):
            return ("BROKER_ORDER_SNAPSHOT_CONFLICT",)
    return ()


def finalize_candidate_execution(
    case: FrozenDecisionCase,
    outcome: CandidateExecutionOutcome,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    *,
    committed_at: str,
) -> CandidateExecutionOutcome:
    """Keep capacity when current broker/funds evidence expires during the journey."""
    if outcome.disposition != "RECONCILED":
        return outcome
    command, scope = case.candidate_execution, case.access_scope
    assert command is not None and scope is not None
    now = datetime.fromisoformat(committed_at)
    proof = (
        ledger.get_decision_event(command.position_event_id, connection)
        if command.position_event_id
        else None
    )
    expired = _current_evidence_expired(command.model_dump(mode="python"), command.cutoff_at, now)
    changed = (
        proof is None
        or proof.result.position is None
        or not ledger.position_snapshot_is_latest(connection, scope, proof.result.position.snapshot)
    )
    if proof is not None and proof.case.position is not None:
        expired = expired or _current_evidence_expired(
            proof.case.position.model_dump(mode="python"), command.cutoff_at, now
        )
    if not expired and not changed:
        return outcome
    history = ledger.candidate_execution_history(connection, scope, command.portfolio_id)
    base = next(
        (
            fact.result.candidate_execution
            for fact in reversed(history)
            if fact.case.candidate_execution is not None
            and fact.case.candidate_execution.confirmation_event_id == command.confirmation_event_id
            and fact.result.candidate_execution is not None
        ),
        None,
    )
    if base is None:
        confirmation = ledger.get_decision_event(command.confirmation_event_id, connection)
        assert confirmation is not None and confirmation.result.candidate_confirmation is not None
        base = CandidateExecutionOutcome(
            disposition="BLOCKED",
            reasons=(),
            portfolio_id=command.portfolio_id,
            plan_id=outcome.plan_id,
            confirmation_id=command.confirmation_event_id,
            reservations=confirmation.result.candidate_confirmation.reservations,
        )
    return base.model_copy(
        update={
            "disposition": "BLOCKED",
            "execution_id": case.decision_event_id,
            "terminal_outcome": None,
            "reasons": (
                "CURRENT_EXECUTION_EVIDENCE_EXPIRED" if expired else "BROKER_FACTS_CHANGED",
            ),
        }
    )
