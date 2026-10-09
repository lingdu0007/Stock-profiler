"""Original synthetic execution journeys through saved frozen cases and reports."""

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
from test_candidate_confirmation import confirmation_payload
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.portfolio.execution_contracts import CandidateExecutionCommand


def freeze_execution(payload: dict[str, Any]) -> None:
    payload["candidate_execution"] = CandidateExecutionCommand.model_validate(
        payload["candidate_execution"]
    ).model_dump(mode="json")
    payload["input"]["candidate_execution"] = deepcopy(payload["candidate_execution"])


def execution_payload(
    settings: Settings, *, choice: str = "ACCEPT", alternate_account_route: bool = False
) -> tuple[dict[str, Any], Any]:
    payload = confirmation_payload(settings, alternate_account_route=alternate_account_route)
    for row in payload["candidate_confirmation"]["choices"]:
        row["choice"] = choice
    payload["input"]["candidate_confirmation"] = deepcopy(payload["candidate_confirmation"])
    accepted = run_frozen_decision_case(
        settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert accepted is not None and accepted.result.candidate_confirmation is not None
    confirmation = payload.pop("candidate_confirmation")
    payload["input"].pop("candidate_confirmation")
    version = "candidate-execution.1.0.0"
    payload["version_bundle"].update(
        case_contract_version=version,
        host_contract_version=version,
        report_projection_contract_version=version,
    )
    payload["business_identity"] = "synthetic-execution:declaration"
    payload["knowledge_cutoff"] = "2042-05-19T16:02:00Z"
    payload["candidate_execution"] = {
        "contract_version": "1.0.0",
        "operation": "DECLARE",
        "portfolio_id": confirmation["portfolio_id"],
        "plan_event_id": confirmation["plan_event_id"],
        "confirmation_event_id": accepted.event_id,
        "idempotency_key": "execution-first",
        "cutoff_at": payload["knowledge_cutoff"],
        "seen_execution_id": None,
        "declaration": {
            "security_id": confirmation["choices"][0]["security_id"],
            "status": "FILLED",
            "broker_order_id": None,
            "declared_at": payload["knowledge_cutoff"],
        },
    }
    freeze_execution(payload)
    return payload, accepted


@pytest.mark.parametrize(
    "status", ["PREPARING", "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "UNABLE"]
)
def test_user_declaration_remains_pending_and_cannot_replace_broker_facts(
    migrated_settings: Settings, status: str
) -> None:
    payload, accepted = execution_payload(migrated_settings)
    payload["candidate_execution"]["declaration"]["status"] = status
    payload["input"]["candidate_execution"] = deepcopy(payload["candidate_execution"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None
    outcome = report.result.candidate_execution
    assert outcome is not None and outcome.disposition == "PENDING_RECONCILIATION"
    assert outcome.declarations[0].status == status
    assert outcome.reservations == accepted.result.candidate_confirmation.reservations
    assert outcome.fills == () and outcome.released_reservation_ids == ()
    assert outcome.terminal_outcome is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("causal-missing", "UNKNOWN"),
        ("causal-foreign", "UNKNOWN"),
        ("issuer-conflict", "UNKNOWN"),
        ("step-missing", "DEVIATION"),
    ],
)
def test_execution_attribution_requires_broker_causality_and_saved_step_review(
    migrated_settings: Settings, change: str, expected: str
) -> None:
    payload, _ = broker_payload(migrated_settings)
    order = payload["candidate_execution"]["orders"][0]
    if change == "causal-missing":
        order["causal_reservation_id"] = None
    elif change == "causal-foreign":
        order["causal_reservation_id"] = "synthetic-foreign-intent"
    elif change == "issuer-conflict":
        order["issuer_id"] = "synthetic-other-issuer"
    else:
        order["step_confirmation_id"] = None
    payload["input"]["candidate_execution"] = deepcopy(payload["candidate_execution"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    assert report.result.candidate_execution.fills[0].classification == expected
    if expected == "UNKNOWN":
        assert report.result.candidate_execution.reservations[0].principal == 700
        assert report.result.candidate_execution.unassociated_commitments[0].principal == 500
    assert run_frozen_decision_case(migrated_settings, payload).report == report


def broker_payload(
    settings: Settings,
    *,
    quantity: str = "20",
    price: str = "9",
    terminal: bool = False,
    order_price: str = "10",
    broker_account_id: str | None = None,
) -> tuple[dict[str, Any], Any]:
    from test_position_state_reconciliation import (
        position_case_payload,
        position_evidence,
        refresh_current_position_evidence,
    )

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    payload, accepted = execution_payload(
        settings, alternate_account_route=broker_account_id is not None
    )
    ledger = DecisionLedger(initialize_runtime_storage(settings).engine)
    with ledger.serialize_case_execution() as connection:
        plan_fact = ledger.get_decision_event(
            payload["candidate_execution"]["plan_event_id"], connection
        )
    assert plan_fact is not None and plan_fact.case.candidate_allocation is not None
    with ledger.serialize_case_execution() as connection:
        risk = ledger.get_decision_event(
            plan_fact.case.candidate_allocation.risk_handoff.concentration_event_id, connection
        )
    assert risk is not None and risk.case.concentration is not None
    snapshot = risk.case.concentration.position_snapshot.model_dump(mode="json")
    snapshot["snapshot_id"] = "synthetic-execution-snapshot"
    snapshot["cutoff_at"] = payload["knowledge_cutoff"]
    refresh_current_position_evidence(snapshot, snapshot["cutoff_at"])
    reservation = accepted.result.candidate_confirmation.reservations[0]
    account = next(
        a
        for a in snapshot["accounts"]
        if a["account_id"] == (broker_account_id or reservation.account_id)
    )
    amount = Decimal(quantity) * Decimal(price)
    remaining = reservation.quantity - Decimal(quantity)
    frozen = Decimal(0) if terminal else remaining * Decimal(order_price)
    evidence = position_evidence("synthetic-broker-execution", cutoff_at=snapshot["cutoff_at"])
    for field in (
        "business_effective_at",
        "source_observed_at",
        "locally_acquired_at",
        "validated_at",
    ):
        evidence[field] = "2042-05-19T16:01:40Z"
    if broker_account_id is not None and broker_account_id != reservation.account_id:
        original_account = next(
            a for a in snapshot["accounts"] if a["account_id"] == reservation.account_id
        )
        transfer = amount + frozen
        for source_account, sign, kind in (
            (original_account, Decimal(-1), "TRANSFER_OUT"),
            (account, Decimal(1), "TRANSFER_IN"),
        ):
            delta = transfer * sign
            for field in ("ledger_cash", "trading_cash", "transferable_cash"):
                source_account["cash_state"][field] = str(
                    Decimal(source_account["cash_state"][field]) + delta
                )
            source_account["account_equity"] = str(
                Decimal(source_account["account_equity"]) + delta
            )
            source_account["ledger_entries"].append(
                {
                    "entry_id": f"synthetic-transfer:{source_account['account_id']}",
                    "entry_type": kind,
                    "security_id": None,
                    "quantity_delta": "0",
                    "cost_basis_delta": "0",
                    "cash_delta": str(delta),
                    "occurred_at": "2042-05-19T16:01:25Z",
                    "evidence": deepcopy(evidence),
                }
            )
    account["ledger_entries"].append(
        {
            "entry_id": "synthetic-execution-fill",
            "entry_type": "FILL",
            "security_id": reservation.security_id,
            "quantity_delta": quantity,
            "cost_basis_delta": str(amount),
            "cash_delta": str(-amount),
            "occurred_at": "2042-05-19T16:01:30Z",
            "evidence": deepcopy(evidence),
        }
    )
    position = deepcopy(account["positions"][0])
    position.update(
        position_id="synthetic-execution-position",
        lifecycle_id="synthetic-execution-life",
        security_id=reservation.security_id,
        issuer_id=reservation.issuer_id,
        total_quantity=quantity,
        broker_sellable_quantity=quantity,
        unsettled_quantity="0",
        frozen_quantity="0",
        restricted_quantity="0",
        open_sell_order_quantity="0",
        reported_cost_basis=str(amount),
        market_price=price,
        evidence=deepcopy(evidence),
    )
    account["positions"].append(position)
    cash = account["cash_state"]
    for field in ("ledger_cash", "trading_cash", "transferable_cash"):
        cash[field] = str(Decimal(cash[field]) - amount - (frozen if field != "ledger_cash" else 0))
    cash["frozen_cash"] = str(Decimal(cash["frozen_cash"]) + frozen)
    order_id = "synthetic-execution-order"
    if not terminal:
        account["open_orders"].append(
            {
                "order_id": order_id,
                "security_id": reservation.security_id,
                "side": "BUY",
                "remaining_quantity": str(remaining),
                "reserved_cash": str(frozen),
                "reserved_cash_semantics": "BROKER_FINAL_RESERVED_CASH",
                "evidence": deepcopy(evidence),
            }
        )
    position_report = run_frozen_decision_case(
        settings,
        position_case_payload(settings, "execution-proof", snapshot),
        clock=GovernanceClock(snapshot["cutoff_at"]),
    ).report
    assert position_report is not None and position_report.result.position is not None
    assert position_report.result.position.disposition == "RECONCILED"
    command = payload["candidate_execution"]
    command.update(
        operation="RECONCILE",
        declaration=None,
        position_event_id=position_report.event_id,
        broker_evidence=deepcopy(evidence),
        funds_evidence=deepcopy(evidence),
        orders=[
            {
                "order_id": order_id,
                "account_id": account["account_id"],
                "security_id": reservation.security_id,
                "issuer_id": reservation.issuer_id,
                "side": "BUY",
                "status": "CANCELLED" if terminal else "OPEN",
                "quantity": str(reservation.quantity),
                "filled_quantity": quantity,
                "remaining_quantity": "0" if terminal else str(remaining),
                "limit_price": order_price,
                "reserved_cash": str(frozen),
                "submitted_at": "2042-05-19T16:01:20Z",
                "causal_reservation_id": reservation.commitment_id,
                "step_confirmation_id": accepted.event_id,
                "evidence": deepcopy(evidence),
            }
        ],
        fills=[
            {
                "entry_id": "synthetic-execution-fill",
                "order_id": order_id,
                "account_id": account["account_id"],
                "price": price,
                "fee_entry_ids": [],
            }
        ],
    )
    freeze_execution(payload)
    return payload, accepted


def test_partial_fill_converts_only_broker_quantity_and_keeps_remaining_capacity(
    migrated_settings: Settings,
) -> None:
    payload, accepted = broker_payload(migrated_settings)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    assert outcome.disposition == "RECONCILED"
    assert outcome.fills[0].classification == "PLANNED"
    assert outcome.fills[0].quantity == 20 and outcome.fills[0].cash_used == 180
    assert outcome.rows[0].filled_quantity == 20 and outcome.rows[0].remaining_quantity == 50
    assert outcome.reservations[0].principal == 500
    assert outcome.reservations[1] == accepted.result.candidate_confirmation.reservations[1]
    assert outcome.terminal_outcome is None


def test_price_improvement_is_held_until_authoritative_funds_reconciliation(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings)
    payload["candidate_execution"]["funds_evidence"] = None
    freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    assert report.result.candidate_execution.reservations[0].principal == 500
    assert report.result.candidate_execution.reservations[0].reconciliation_cash_hold == 20
    assert report.result.candidate_execution.terminal_outcome is None


def test_linked_order_and_plan_share_one_stricter_capacity_claim(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings, order_price="12")
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    assert outcome.reservations[0].principal == 600
    assert outcome.reservations[0].broker_order_id == "synthetic-execution-order"
    assert outcome.unassociated_commitments == ()
    assert outcome.fills[0].classification == "DEVIATION"


@pytest.mark.parametrize(
    "change",
    [
        "duplicate-fill",
        "duplicate-order",
        "omit-fill",
        "omit-order",
        "quantity-conflict",
        "cash-conflict",
        "foreign-account",
        "unknown-order",
    ],
)
def test_incomplete_or_conflicting_broker_facts_cannot_move_capacity(
    migrated_settings: Settings,
    change: str,
) -> None:
    payload, _ = broker_payload(migrated_settings)
    command = payload["candidate_execution"]
    if change == "duplicate-fill":
        command["fills"].append(deepcopy(command["fills"][0]))
    elif change == "duplicate-order":
        command["orders"].append(deepcopy(command["orders"][0]))
    elif change == "omit-fill":
        command["fills"] = []
    elif change == "omit-order":
        command["orders"] = []
    elif change == "quantity-conflict":
        command["orders"][0]["filled_quantity"] = "40"
    elif change == "cash-conflict":
        command["orders"][0]["reserved_cash"] = "0"
    elif change == "foreign-account":
        command["orders"][0]["account_id"] = "synthetic-foreign-account"
    else:
        command["orders"][0]["remaining_quantity"] = None
        command["orders"][0]["status"] = "UNKNOWN"
    freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    assert report.result.candidate_execution.disposition == "BLOCKED"
    assert report.result.candidate_execution.released_reservation_ids == ()


def test_later_declaration_retains_reconciled_fills_and_capacity(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert first is not None and first.result.candidate_execution is not None
    command = payload["candidate_execution"]
    command.update(
        operation="DECLARE",
        idempotency_key="execution-later",
        seen_execution_id=first.event_id,
        declaration={
            "security_id": command["orders"][0]["security_id"],
            "status": "CANCELLED",
            "broker_order_id": command["orders"][0]["order_id"],
            "declared_at": payload["knowledge_cutoff"],
        },
    )
    freeze_execution(payload)
    second = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert second is not None and second.result.candidate_execution is not None
    outcome = second.result.candidate_execution
    assert outcome.reservations == first.result.candidate_execution.reservations
    assert outcome.fills == first.result.candidate_execution.fills
    assert outcome.orders == first.result.candidate_execution.orders
    assert outcome.rows == first.result.candidate_execution.rows
    assert outcome.declarations[-1].status == "CANCELLED"
    assert run_frozen_decision_case(migrated_settings, payload).report == second


def test_stale_execution_and_idempotency_conflict_cannot_reset_capacity(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.modules.decision_cases.ports import DecisionEventCommitError

    payload, _ = broker_payload(migrated_settings)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert first is not None and first.result.candidate_execution is not None
    conflict = deepcopy(payload)
    conflict["candidate_execution"]["orders"][0]["causal_reservation_id"] = None
    freeze_execution(conflict)
    with pytest.raises(DecisionEventCommitError, match="idempotency"):
        run_frozen_decision_case(migrated_settings, conflict)
    payload["candidate_execution"]["idempotency_key"] = "execution-stale"
    freeze_execution(payload)
    stale = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert stale is not None and stale.result.candidate_execution is not None
    assert stale.result.candidate_execution.disposition == "BLOCKED"
    assert "EXECUTION_VERSION_CONFLICT" in stale.result.candidate_execution.reasons
    assert (
        stale.result.candidate_execution.reservations
        == first.result.candidate_execution.reservations
    )


@pytest.mark.parametrize("terminal", [False, True])
def test_explicit_withdrawal_releases_only_closed_broker_orders(
    migrated_settings: Settings, terminal: bool
) -> None:
    payload, accepted = broker_payload(migrated_settings, terminal=terminal)
    payload["candidate_execution"]["withdrawn_reservation_ids"] = [
        row.commitment_id for row in accepted.result.candidate_confirmation.reservations
    ]
    payload["input"]["candidate_execution"] = deepcopy(payload["candidate_execution"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    if terminal:
        assert outcome.disposition == "RECONCILED"
        assert outcome.reservations == ()
        assert len(outcome.released_reservation_ids) == 2
        assert outcome.terminal_outcome == "PARTIALLY_FILLED"
    else:
        assert outcome.disposition == "BLOCKED"
        assert outcome.released_reservation_ids == ()
        assert outcome.reservations == accepted.result.candidate_confirmation.reservations


def test_full_fill_without_funds_proof_retains_cash_hold(migrated_settings: Settings) -> None:
    payload, _ = broker_payload(migrated_settings, quantity="70", terminal=True)
    payload["candidate_execution"]["funds_evidence"] = None
    freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    assert outcome.rows[0].state == "FILLED"
    assert outcome.reservations[0].principal == 0
    assert outcome.reservations[0].reconciliation_cash_hold == 70
    assert outcome.terminal_outcome is None


def append_broker_adjustment(
    settings: Settings, payload: dict[str, Any], *, correction: bool
) -> None:
    from test_position_state_reconciliation import position_case_payload

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    command = payload["candidate_execution"]
    ledger = DecisionLedger(initialize_runtime_storage(settings).engine)
    with ledger.serialize_case_execution() as connection:
        proof = ledger.get_decision_event(command["position_event_id"], connection)
    assert proof is not None and proof.case.position is not None
    snapshot = proof.case.position.model_dump(mode="json")
    snapshot["snapshot_id"] += ":adjustment"
    order = command["orders"][0]
    account = next(a for a in snapshot["accounts"] if a["account_id"] == order["account_id"])
    entry = deepcopy(account["ledger_entries"][-1])
    entry.update(entry_id="synthetic-broker-adjustment", occurred_at="2042-05-19T16:01:35Z")
    if correction:
        entry.update(
            quantity_delta="-5",
            cost_basis_delta="-45",
            cash_delta="45",
            corrects_entry_id=entry["entry_id"].replace("adjustment", "execution-fill"),
            correction_reason="Synthetic broker correction",
        )
        entry["corrects_entry_id"] = "synthetic-execution-fill"
        quantity = Decimal(order["filled_quantity"]) - 5
        opened = order["status"] == "OPEN"
        remaining = Decimal(order["quantity"]) - quantity if opened else Decimal(0)
        order.update(
            filled_quantity=str(quantity),
            remaining_quantity=str(remaining),
            reserved_cash=str(remaining * 10),
            status="OPEN" if opened else "CANCELLED",
        )
        if opened:
            account["open_orders"][-1].update(
                remaining_quantity=str(remaining), reserved_cash=str(remaining * 10)
            )
            account["cash_state"]["frozen_cash"] = str(
                Decimal(account["cash_state"]["frozen_cash"]) + 50
            )
        account["positions"][-1].update(
            total_quantity=str(quantity),
            broker_sellable_quantity=str(quantity),
            reported_cost_basis=str(quantity * 9),
        )
        command["fills"].append(
            {
                "entry_id": entry["entry_id"],
                "order_id": order["order_id"],
                "account_id": order["account_id"],
                "price": "9",
                "fee_entry_ids": [],
            }
        )
        adjustments = {
            "ledger_cash": Decimal(45),
            "trading_cash": Decimal(-5) if opened else Decimal(45),
            "transferable_cash": Decimal(-5) if opened else Decimal(45),
        }
    else:
        entry.update(entry_type="FEE", quantity_delta="0", cost_basis_delta="2", cash_delta="-2")
        account["positions"][-1]["reported_cost_basis"] = "182"
        account["account_equity"] = str(Decimal(account["account_equity"]) - 2)
        command["fills"][0]["fee_entry_ids"] = [entry["entry_id"]]
        adjustments = {
            field: Decimal(-2) for field in ("ledger_cash", "trading_cash", "transferable_cash")
        }
    account["ledger_entries"].append(entry)
    for field, delta in adjustments.items():
        account["cash_state"][field] = str(Decimal(account["cash_state"][field]) + delta)
    report = run_frozen_decision_case(
        settings,
        position_case_payload(settings, "execution-adjustment", snapshot),
        clock=GovernanceClock(payload["knowledge_cutoff"]),
    ).report
    assert (
        report is not None
        and report.result.position is not None
        and report.result.position.disposition == "RECONCILED"
    )
    command["position_event_id"] = report.event_id
    freeze_execution(payload)


@pytest.mark.parametrize("omit", [False, True])
def test_broker_fees_are_unique_authoritative_cash_facts(
    migrated_settings: Settings, omit: bool
) -> None:
    payload, _ = broker_payload(migrated_settings)
    append_broker_adjustment(migrated_settings, payload, correction=False)
    if omit:
        payload["candidate_execution"]["fills"][0]["fee_entry_ids"] = []
        freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    if omit:
        assert outcome.disposition == "BLOCKED"
    else:
        assert outcome.disposition == "RECONCILED"
        assert outcome.fills[0].fees == 2
        assert outcome.fills[0].cash_used == 182


def test_authoritative_correction_is_additive_and_preserves_original_report(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings)
    old_payload = deepcopy(payload)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert first is not None and first.result.candidate_execution is not None
    append_broker_adjustment(migrated_settings, payload, correction=True)
    payload["candidate_execution"].update(
        idempotency_key="execution-correction", seen_execution_id=first.event_id
    )
    freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    assert outcome.disposition == "RECONCILED"
    assert len(outcome.fills) == 2
    assert outcome.rows[0].filled_quantity == 15
    assert outcome.reservations[0].principal == 550
    assert run_frozen_decision_case(migrated_settings, old_payload).report == first


def forward_allocation_payload(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    from test_position_state_reconciliation import position_evidence

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    command = payload["candidate_execution"]
    ledger = DecisionLedger(initialize_runtime_storage(settings).engine)
    with ledger.serialize_case_execution() as connection:
        plan = ledger.get_decision_event(command["plan_event_id"], connection)
        proof = ledger.get_decision_event(command["position_event_id"], connection)
    assert plan is not None and plan.case.candidate_allocation is not None
    assert proof is not None and proof.case.position is not None
    snapshot = proof.case.position.model_dump(mode="json")
    later = plan.case.model_dump(mode="json")
    allocation = later["candidate_allocation"]
    refs = allocation["risk_handoff"]
    cutoff = command["cutoff_at"]
    for name in ("concentration", "stress", "liquidity", "drawdown"):
        with ledger.serialize_case_execution() as connection:
            fact = ledger.get_decision_event(refs[f"{name}_event_id"], connection)
        assert fact is not None
        risk = fact.case.model_dump(mode="json")
        risk["business_identity"] += ":forward-execution"
        risk["knowledge_cutoff"] = cutoff
        if name == "drawdown":
            assert fact.result.drawdown is not None and fact.result.drawdown.state is not None
            before_valuation = deepcopy(risk[name]["valuation"])
            processed = {
                (key.account_id, key.entry_id)
                for key in fact.result.drawdown.state.processed_transfers
            }
            transfers: dict[str, list[dict[str, str]]] = {}
            for account in snapshot["accounts"]:
                for entry in account["ledger_entries"]:
                    key = (account["account_id"], entry["entry_id"])
                    if (
                        entry["entry_type"] in {"TRANSFER_IN", "TRANSFER_OUT"}
                        and key not in processed
                    ):
                        transfers.setdefault(entry["occurred_at"], []).append(
                            {"account_id": key[0], "entry_id": key[1]}
                        )
            risk[name].update(
                operation="OBSERVE",
                previous_decision_id=fact.decision_event_id,
                cutoff_at=cutoff,
                policy=fact.result.drawdown.state.policy.model_dump(mode="json"),
                capital_flows=[
                    {
                        "flow_id": f"synthetic-execution-transfer:{occurred_at}",
                        "kind": "INTERNAL",
                        "occurred_at": occurred_at,
                        "before_valuation": before_valuation,
                        "ledger_keys": keys,
                    }
                    for occurred_at, keys in sorted(transfers.items())
                ],
                valuation={
                    "position_event_id": proof.decision_event_id,
                    "liquidation_cost": "0",
                    "evidence": snapshot["snapshot_evidence"],
                },
            )
        else:
            risk[name]["position_snapshot"] = deepcopy(snapshot)
            if name == "concentration":
                for cost in risk[name]["liquidation_costs"]:
                    cost["evidence"] = position_evidence("synthetic-current-cost", cutoff_at=cutoff)
            if name == "liquidity":
                risk[name]["cost_evidence"] = snapshot["snapshot_evidence"]
                template = risk[name]["sale_terms"][0]
                risk[name]["sale_terms"] = [
                    dict(
                        deepcopy(template),
                        account_id=a["account_id"],
                        security_id=p["security_id"],
                        evidence=snapshot["snapshot_evidence"],
                    )
                    for a in snapshot["accounts"]
                    for p in a["positions"]
                ]
        report = run_frozen_decision_case(settings, risk, clock=GovernanceClock(cutoff)).report
        assert report is not None
        outcome = getattr(report.result, name)
        assert outcome is not None and getattr(
            outcome, "disposition", getattr(outcome, "state", None)
        ) in {"ASSESSED", "AVAILABLE", "ACCEPTED", "NORMAL"}, outcome
        refs[f"{name}_event_id"] = report.event_id
    refs["cutoff_at"] = cutoff
    allocation["cutoff_at"] = cutoff
    for group in ("securities", "routes"):
        for item in allocation[group]:
            item["evidence"] = position_evidence("synthetic-forward-input", cutoff_at=cutoff)
    allocation["correlations"]["evidence"] = position_evidence(
        "synthetic-forward-correlations", cutoff_at=cutoff
    )
    later["knowledge_cutoff"] = cutoff
    later["business_identity"] += ":forward-execution"
    from stock_profiler.modules.portfolio.allocation_contracts import CandidateAllocationCommand

    later["candidate_allocation"] = CandidateAllocationCommand.model_validate(
        allocation
    ).model_dump(mode="json")
    later["input"]["candidate_allocation"] = deepcopy(later["candidate_allocation"])
    return later


def test_next_allocation_consumes_actual_fill_and_one_remaining_order_claim(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert execution is not None
    later = forward_allocation_payload(migrated_settings, payload)
    report = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_allocation is not None
    outcome = report.result.candidate_allocation
    assert outcome.disposition == "PLANNED", outcome.reasons
    claims = {row.commitment_id: row for row in outcome.commitments}
    reservation_id = payload["candidate_execution"]["orders"][0]["causal_reservation_id"]
    assert claims[reservation_id].principal == 500
    assert len(claims) == 2
    assert outcome.rows[0].committed_exposure == 680
    assert outcome.total_principal == 0


def test_saved_broker_attribution_cannot_be_reassigned_by_a_later_request(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert first is not None and first.result.candidate_execution is not None
    payload["candidate_execution"].update(
        idempotency_key="execution-reassign", seen_execution_id=first.event_id
    )
    payload["candidate_execution"]["orders"][0]["causal_reservation_id"] = None
    freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    assert report.result.candidate_execution.disposition == "BLOCKED"
    assert report.result.candidate_execution.fills == first.result.candidate_execution.fills


def test_actual_fill_price_above_cap_is_provable_execution_deviation(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings, price="11")
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    assert report.result.candidate_execution.fills[0].classification == "DEVIATION"
    assert "FILL_PRICE_DEVIATION" in report.result.candidate_execution.fills[0].reasons


def test_authoritative_external_origin_distinguishes_autonomous_same_security_buy(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings)
    payload["candidate_execution"]["orders"][0].update(
        causal_reservation_id=None, external_origin_id="synthetic-broker-manual-origin"
    )
    payload["input"]["candidate_execution"] = deepcopy(payload["candidate_execution"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    assert report.result.candidate_execution.fills[0].classification == "EXTERNAL"
    assert report.result.candidate_execution.fills[0].reservation_id is None


def test_broker_surplus_remains_external_to_the_accepted_intent(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings, quantity="80", terminal=True)
    payload["candidate_execution"]["orders"][0]["quantity"] = "80"
    freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    assert outcome.disposition == "RECONCILED"
    assert outcome.fills[0].quantity == 80
    assert outcome.fills[0].intent_quantity == 70
    assert outcome.fills[0].external_quantity == 10
    assert outcome.rows[0].filled_quantity == 70


def refresh_broker_cutoff(settings: Settings, payload: dict[str, Any], cutoff: str) -> None:
    from test_position_state_reconciliation import (
        position_case_payload,
        position_evidence,
        refresh_current_position_evidence,
    )

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    command = payload["candidate_execution"]
    ledger = DecisionLedger(initialize_runtime_storage(settings).engine)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_decision_event(command["position_event_id"], connection)
    assert fact is not None and fact.case.position is not None
    snapshot = fact.case.position.model_dump(mode="json")
    snapshot.update(cutoff_at=cutoff, snapshot_id="synthetic-execution-forward-snapshot")
    refresh_current_position_evidence(snapshot, cutoff)
    report = run_frozen_decision_case(
        settings,
        position_case_payload(settings, "execution-forward-snapshot", snapshot),
        clock=GovernanceClock(cutoff),
    ).report
    assert (
        report is not None
        and report.result.position is not None
        and report.result.position.disposition == "RECONCILED"
    )
    payload["knowledge_cutoff"] = cutoff
    command.update(
        cutoff_at=cutoff,
        position_event_id=report.event_id,
        broker_evidence=position_evidence("synthetic-execution-broker", cutoff_at=cutoff),
        funds_evidence=position_evidence("synthetic-execution-funds", cutoff_at=cutoff),
    )
    for order in command["orders"]:
        order["evidence"] = position_evidence("synthetic-current-order", cutoff_at=cutoff)
    freeze_execution(payload)


@pytest.mark.parametrize("unknown", [False, True])
def test_entry_expiry_retains_open_or_unknown_orders(
    migrated_settings: Settings, unknown: bool
) -> None:
    payload, _ = broker_payload(migrated_settings)
    refresh_broker_cutoff(migrated_settings, payload, "2042-05-26T16:02:00Z")
    if unknown:
        payload["candidate_execution"]["orders"][0]["status"] = "UNKNOWN"
        freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    assert outcome.terminal_outcome is None
    assert outcome.reservations[0].principal == (700 if unknown else 500)
    assert outcome.disposition == ("BLOCKED" if unknown else "RECONCILED")


@pytest.mark.parametrize(
    ("choice", "expired", "expected"),
    [
        ("DECLINE", False, "ALL_DECLINED"),
        ("DEFER", False, None),
        ("DEFER", True, "DEFERRED_EXPIRED"),
        ("ACCEPT", False, "NO_TRADE"),
    ],
)
def test_plan_terminal_requires_all_choices_and_authoritative_no_order_proof(
    migrated_settings: Settings, choice: str, expired: bool, expected: str | None
) -> None:
    from test_position_state_reconciliation import (
        position_case_payload,
        position_evidence,
        refresh_current_position_evidence,
    )

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    payload, accepted = execution_payload(migrated_settings, choice=choice)
    command = payload["candidate_execution"]
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        plan = ledger.get_decision_event(command["plan_event_id"], connection)
    assert plan is not None and plan.case.candidate_allocation is not None
    with ledger.serialize_case_execution() as connection:
        risk = ledger.get_decision_event(
            plan.case.candidate_allocation.risk_handoff.concentration_event_id, connection
        )
    assert risk is not None and risk.case.concentration is not None
    snapshot = risk.case.concentration.position_snapshot.model_dump(mode="json")
    cutoff = "2042-05-26T16:02:00Z" if expired else payload["knowledge_cutoff"]
    snapshot.update(snapshot_id="synthetic-no-order-proof", cutoff_at=cutoff)
    refresh_current_position_evidence(snapshot, cutoff)
    proof = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "no-order-proof", snapshot),
        clock=GovernanceClock(cutoff),
    ).report
    assert (
        proof is not None
        and proof.result.position is not None
        and proof.result.position.disposition == "RECONCILED"
    )
    command.update(
        operation="RECONCILE",
        declaration=None,
        cutoff_at=cutoff,
        position_event_id=proof.event_id,
        broker_evidence=position_evidence("synthetic-no-orders", cutoff_at=cutoff),
        funds_evidence=position_evidence("synthetic-no-order-funds", cutoff_at=cutoff),
        withdrawn_reservation_ids=[
            row.commitment_id for row in accepted.result.candidate_confirmation.reservations
        ],
    )
    payload["knowledge_cutoff"] = cutoff
    freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(cutoff)
    ).report
    assert report is not None and report.result.candidate_execution is not None
    assert report.result.candidate_execution.disposition == "RECONCILED"
    assert report.result.candidate_execution.terminal_outcome == expected


def test_followup_reconciliation_cannot_revive_an_explicitly_withdrawn_intent(
    migrated_settings: Settings,
) -> None:
    payload, accepted = broker_payload(migrated_settings, terminal=True)
    payload["candidate_execution"]["withdrawn_reservation_ids"] = [
        r.commitment_id for r in accepted.result.candidate_confirmation.reservations
    ]
    freeze_execution(payload)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert first is not None and first.result.candidate_execution is not None
    assert first.result.candidate_execution.reservations == ()
    payload["candidate_execution"].update(
        idempotency_key="execution-after-withdrawal",
        seen_execution_id=first.event_id,
        withdrawn_reservation_ids=[],
    )
    freeze_execution(payload)
    later = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert later is not None and later.result.candidate_execution is not None
    assert later.result.candidate_execution.reservations == ()
    assert later.result.candidate_execution.terminal_outcome == "PARTIALLY_FILLED"


def test_declaration_cannot_clear_an_authoritative_reconciliation_gap(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings)
    payload["candidate_execution"]["orders"][0]["status"] = "UNKNOWN"
    freeze_execution(payload)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert first is not None and first.result.candidate_execution is not None
    assert first.result.candidate_execution.disposition == "BLOCKED"
    payload["candidate_execution"].update(
        operation="DECLARE",
        idempotency_key="execution-after-gap",
        seen_execution_id=first.event_id,
        declaration={
            "security_id": payload["candidate_execution"]["orders"][0]["security_id"],
            "status": "CANCELLED",
            "broker_order_id": None,
            "declared_at": payload["knowledge_cutoff"],
        },
    )
    freeze_execution(payload)
    later = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert later is not None and later.result.candidate_execution is not None
    assert later.result.candidate_execution.reasons == first.result.candidate_execution.reasons


def test_generic_user_fact_cannot_change_a_candidate_execution_report(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.modules.delivery.access import AccessPrincipal
    from stock_profiler.modules.delivery.user_facts import UserFactRequest

    payload, _ = broker_payload(migrated_settings)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.access_scope is not None
    principal = AccessPrincipal(
        user_id=report.access_scope.user_id,
        account_ids=report.access_scope.account_ids,
        permissions=("REPORT_READ", "USER_FACT"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    assert (
        delivery.record_user_fact(
            report.report_version_id,
            principal,
            UserFactRequest(kind="CONFIRMED", choice="ACCEPT", idempotency_key="generic-execution"),
        )
        is None
    )
    assert delivery.audit_history()[-1].reason == "CANDIDATE_READ_ONLY"
    assert delivery.read_report(report.report_version_id, principal) == report


@pytest.mark.parametrize("fault", ["before-commit", "after-commit"])
def test_execution_commit_and_retry_preserve_one_atomic_fact(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
    from stock_profiler.modules.decision_cases.ports import (
        DecisionEventCommitError,
        DecisionEventCommitUncertainError,
    )

    payload, _ = broker_payload(migrated_settings)
    original = DecisionLedger.commit_event

    def commit(self: DecisionLedger, *args: Any, **kwargs: Any) -> Any:
        if fault == "after-commit":
            original(self, *args, **kwargs)
            raise DecisionEventCommitUncertainError("Synthetic lost acknowledgement")
        raise DecisionEventCommitError("Synthetic rollback")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", commit)
        attempt = run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
        )
    case = FrozenDecisionCase.model_validate(payload)
    assert case.access_scope is not None
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        facts = ledger.candidate_execution_history(
            connection, case.access_scope, payload["candidate_execution"]["portfolio_id"]
        )
    assert len(facts) == (0 if fault == "before-commit" else 1)
    if fault == "before-commit":
        assert attempt.report is None
    recovered = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert recovered.report is not None and recovered.report.result.candidate_execution is not None
    assert recovered.report.result.candidate_execution.reservations[0].principal == 500
    assert recovered.framework_run_id == attempt.framework_run_id
    with ledger.serialize_case_execution() as connection:
        assert (
            len(
                ledger.candidate_execution_history(
                    connection, case.access_scope, payload["candidate_execution"]["portfolio_id"]
                )
            )
            == 1
        )


def test_cli_replays_saved_execution_with_the_same_framework_run(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    import sys

    from stock_profiler.entrypoints.cli import main

    payload, _ = broker_payload(migrated_settings)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert first.report is not None
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: migrated_settings)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stock-profiler",
            "decision-case-replay",
            "--business-identity",
            payload["business_identity"],
        ],
    )
    main()
    replay = json.loads(capsys.readouterr().out)
    assert replay["framework_run_id"] == first.framework_run_id
    assert replay["report"] == first.report.model_dump(mode="json")


def test_expired_unattributed_order_without_fills_keeps_both_capacity_claims(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings, quantity="0")
    payload["candidate_execution"]["fills"] = []
    payload["candidate_execution"]["orders"][0]["causal_reservation_id"] = None
    refresh_broker_cutoff(migrated_settings, payload, "2042-05-26T16:02:00Z")
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    assert outcome.reservations[0].principal == 700
    assert outcome.unassociated_commitments[0].principal == 700
    assert outcome.rows[0].state == "UNKNOWN"
    assert outcome.terminal_outcome is None


def test_multiple_broker_orders_share_the_stricter_remaining_intent_claim(
    migrated_settings: Settings,
) -> None:
    from test_position_state_reconciliation import position_case_payload

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    payload, _ = broker_payload(migrated_settings, order_price="12")
    command = payload["candidate_execution"]
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_decision_event(command["position_event_id"], connection)
    assert fact is not None and fact.case.position is not None
    snapshot = fact.case.position.model_dump(mode="json")
    account = next(
        a for a in snapshot["accounts"] if a["account_id"] == command["orders"][0]["account_id"]
    )
    account["open_orders"][-1].update(remaining_quantity="25", reserved_cash="300")
    second = deepcopy(account["open_orders"][-1])
    second["order_id"] += ":second"
    account["open_orders"].append(second)
    command["orders"][0].update(quantity="45", remaining_quantity="25", reserved_cash="300")
    second_order = deepcopy(command["orders"][0])
    second_order.update(order_id=second["order_id"], quantity="25", filled_quantity="0")
    command["orders"].append(second_order)
    proof = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "split-order-proof", snapshot),
        clock=GovernanceClock(payload["knowledge_cutoff"]),
    ).report
    assert (
        proof is not None
        and proof.result.position is not None
        and proof.result.position.disposition == "RECONCILED"
    )
    command["position_event_id"] = proof.event_id
    freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    assert outcome.reservations[0].principal == 600
    assert len(outcome.reservations[0].broker_order_ids) == 2
    assert outcome.unassociated_commitments == ()
    later = forward_allocation_payload(migrated_settings, payload)
    allocated = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert allocated is not None and allocated.result.candidate_allocation is not None
    assert allocated.result.candidate_allocation.disposition == "PLANNED", (
        allocated.result.candidate_allocation.reasons
    )
    assert len(allocated.result.candidate_allocation.commitments) == 2
    assert allocated.result.candidate_allocation.rows[0].committed_exposure == 780


def test_cash_hold_is_released_only_after_funds_and_never_expands_accepted_quantity(
    migrated_settings: Settings,
) -> None:
    payload, accepted = broker_payload(migrated_settings, terminal=True)
    funds = deepcopy(payload["candidate_execution"]["funds_evidence"])
    payload["candidate_execution"].update(
        funds_evidence=None,
        withdrawn_reservation_ids=[
            r.commitment_id for r in accepted.result.candidate_confirmation.reservations
        ],
    )
    freeze_execution(payload)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert first is not None and first.result.candidate_execution is not None
    assert first.result.candidate_execution.reservations[0].reconciliation_cash_hold == 20
    assert (
        first.result.candidate_execution.reservations[0].commitment_id
        not in first.result.candidate_execution.released_reservation_ids
    )
    later = forward_allocation_payload(migrated_settings, payload)
    held = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert held is not None and held.result.candidate_allocation is not None
    assert held.result.candidate_allocation.disposition == "PLANNED", (
        held.result.candidate_allocation.reasons
    )
    payload["candidate_execution"].update(
        idempotency_key="execution-funds-cleared",
        seen_execution_id=first.event_id,
        funds_evidence=funds,
        withdrawn_reservation_ids=[],
    )
    freeze_execution(payload)
    cleared = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert cleared is not None and cleared.result.candidate_execution is not None
    assert cleared.result.candidate_execution.reservations == ()
    assert cleared.result.candidate_execution.rows[0].accepted_quantity == 70
    later["business_identity"] += ":funds-cleared"
    new_plan = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert new_plan is not None and new_plan.result.candidate_allocation is not None
    assert (
        new_plan.result.candidate_allocation.total_principal
        == held.result.candidate_allocation.total_principal
        == 1200
    )
    assert new_plan.result.candidate_allocation.remaining_cash is not None
    assert held.result.candidate_allocation.remaining_cash is not None
    assert (
        new_plan.result.candidate_allocation.remaining_cash
        - held.result.candidate_allocation.remaining_cash
        == 20
    )


def test_all_accepted_intents_filled_and_reconciled_form_full_fill_terminal(
    migrated_settings: Settings,
) -> None:
    from test_position_state_reconciliation import position_case_payload

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    payload, accepted = broker_payload(migrated_settings, quantity="70", terminal=True)
    command = payload["candidate_execution"]
    command["orders"][0]["status"] = "FILLED"
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_decision_event(command["position_event_id"], connection)
    assert fact is not None and fact.case.position is not None
    snapshot = fact.case.position.model_dump(mode="json")
    reservation = accepted.result.candidate_confirmation.reservations[1]
    account = next(a for a in snapshot["accounts"] if a["account_id"] == reservation.account_id)
    original_account = next(
        a for a in snapshot["accounts"] if a["account_id"] == command["orders"][0]["account_id"]
    )
    entry = deepcopy(original_account["ledger_entries"][-1])
    entry.update(entry_id="synthetic-second-fill", security_id=reservation.security_id)
    position = deepcopy(original_account["positions"][-1])
    position.update(
        position_id="synthetic-second-position",
        lifecycle_id="synthetic-second-life",
        security_id=reservation.security_id,
        issuer_id=reservation.issuer_id,
    )
    account["ledger_entries"].append(entry)
    account["positions"].append(position)
    for field in ("ledger_cash", "trading_cash", "transferable_cash"):
        account["cash_state"][field] = str(Decimal(account["cash_state"][field]) - 630)
    order = deepcopy(command["orders"][0])
    order.update(
        order_id="synthetic-second-order",
        account_id=reservation.account_id,
        security_id=reservation.security_id,
        issuer_id=reservation.issuer_id,
        causal_reservation_id=reservation.commitment_id,
    )
    command["orders"].append(order)
    fill = deepcopy(command["fills"][0])
    fill.update(
        entry_id=entry["entry_id"], order_id=order["order_id"], account_id=reservation.account_id
    )
    command["fills"].append(fill)
    proof = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "full-fill-proof", snapshot),
        clock=GovernanceClock(payload["knowledge_cutoff"]),
    ).report
    assert (
        proof is not None
        and proof.result.position is not None
        and proof.result.position.disposition == "RECONCILED"
    )
    command["position_event_id"] = proof.event_id
    freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    assert report.result.candidate_execution.reservations == ()
    assert report.result.candidate_execution.terminal_outcome == "FULLY_FILLED"
    assert all(
        row.filled_quantity == row.accepted_quantity
        for row in report.result.candidate_execution.rows
    )


def test_updated_position_correction_requires_execution_reconciliation_before_new_allocation(
    migrated_settings: Settings,
) -> None:
    payload, _ = broker_payload(migrated_settings, quantity="70", terminal=True)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert first is not None
    append_broker_adjustment(migrated_settings, payload, correction=True)
    later = forward_allocation_payload(migrated_settings, payload)
    report = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.disposition == "BLOCKED"
    assert "EXECUTION_POSITION_NOT_RECONCILED" in report.result.candidate_allocation.reasons


def test_capacity_release_rechecks_funds_evidence_at_the_actual_commit_boundary(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger

    payload, accepted = broker_payload(migrated_settings, terminal=True)
    payload["candidate_execution"]["withdrawn_reservation_ids"] = [
        r.commitment_id for r in accepted.result.candidate_confirmation.reservations
    ]
    payload["candidate_execution"]["funds_evidence"]["expires_at"] = "2042-05-19T16:02:30Z"
    freeze_execution(payload)
    clock = GovernanceClock(payload["knowledge_cutoff"])
    original = DecisionLedger.record_stage_result

    def record(self: DecisionLedger, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        if kwargs["stage_result"].phase == "CANDIDATE_EXECUTION":
            clock.current = datetime.fromisoformat("2042-05-19T16:03:00+00:00")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "record_stage_result", record)
        report = run_frozen_decision_case(migrated_settings, payload, clock=clock).report
    assert report is not None and report.result.candidate_execution is not None
    assert report.result.candidate_execution.disposition == "BLOCKED"
    assert (
        report.result.candidate_execution.reservations
        == accepted.result.candidate_confirmation.reservations
    )
    assert report.result.candidate_execution.released_reservation_ids == ()


def test_successive_plans_share_broker_history_without_reassigning_intents(
    migrated_settings: Settings,
) -> None:
    from test_position_state_reconciliation import (
        position_case_payload,
        position_evidence,
        refresh_current_position_evidence,
    )

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
    from stock_profiler.modules.portfolio.confirmation_contracts import CandidateConfirmationCommand

    payload, accepted = broker_payload(migrated_settings, terminal=True)
    command = payload["candidate_execution"]
    command["withdrawn_reservation_ids"] = [
        r.commitment_id for r in accepted.result.candidate_confirmation.reservations
    ]
    freeze_execution(payload)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert first is not None and first.result.candidate_execution is not None
    command = payload["candidate_execution"]
    later = forward_allocation_payload(migrated_settings, payload)
    second_plan = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert second_plan is not None and second_plan.result.candidate_allocation is not None
    plan = second_plan.result.candidate_allocation
    assert plan.disposition == "PLANNED" and plan.total_principal == 1200
    submission = deepcopy(later)
    allocation = submission.pop("candidate_allocation")
    submission["input"].pop("candidate_allocation")
    submission["business_identity"] = "synthetic-confirmation:second-plan"
    submission["version_bundle"].update(
        case_contract_version="candidate-confirmation.1.0.0",
        host_contract_version="candidate-confirmation.1.0.0",
        report_projection_contract_version="candidate-confirmation.1.0.0",
    )
    submission["candidate_confirmation"] = CandidateConfirmationCommand.model_validate(
        {
            "contract_version": "1.0.0",
            "operation": "SUBMIT",
            "user_id": submission["access_scope"]["user_id"],
            "portfolio_id": command["portfolio_id"],
            "candidate_batch_id": plan.candidate_batch_id,
            "plan_event_id": second_plan.event_id,
            "plan_id": plan.plan_id,
            "seen_confirmation_id": None,
            "idempotency_key": "second-plan-confirmation",
            "withdrawal_position_event_id": None,
            "choices": [
                {"security_id": r.candidate.security_id, "choice": "ACCEPT"}
                for r in plan.rows
                if r.principal
            ],
            "revalidation": allocation,
        }
    ).model_dump(mode="json")
    submission["input"]["candidate_confirmation"] = deepcopy(submission["candidate_confirmation"])
    second_confirmation = run_frozen_decision_case(
        migrated_settings, submission, clock=GovernanceClock("2042-05-19T16:02:20Z")
    ).report
    assert (
        second_confirmation is not None
        and second_confirmation.result.candidate_confirmation is not None
    )
    assert second_confirmation.result.candidate_confirmation.disposition == "CONFIRMED", (
        second_confirmation.result.candidate_confirmation.reasons
    )
    reserve = second_confirmation.result.candidate_confirmation.reservations[0]
    assert reserve.quantity == 50
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_decision_event(command["position_event_id"], connection)
    assert fact is not None and fact.case.position is not None
    snapshot = fact.case.position.model_dump(mode="json")
    cutoff = "2042-05-19T16:03:00Z"
    snapshot.update(snapshot_id="synthetic-second-plan-execution", cutoff_at=cutoff)
    refresh_current_position_evidence(snapshot, cutoff)
    account = next(a for a in snapshot["accounts"] if a["account_id"] == reserve.account_id)
    entry = deepcopy(account["ledger_entries"][-1])
    entry.update(
        entry_id="synthetic-second-plan-fill",
        quantity_delta="10",
        cost_basis_delta="90",
        cash_delta="-90",
        occurred_at="2042-05-19T16:02:50Z",
        evidence=position_evidence("synthetic-second-plan-fill", cutoff_at=cutoff),
    )
    for field in (
        "business_effective_at",
        "source_observed_at",
        "locally_acquired_at",
        "validated_at",
    ):
        entry["evidence"][field] = cutoff
    account["ledger_entries"].append(entry)
    account["positions"][-1].update(
        total_quantity="30", broker_sellable_quantity="30", reported_cost_basis="270"
    )
    for field in ("ledger_cash", "trading_cash", "transferable_cash"):
        account["cash_state"][field] = str(
            Decimal(account["cash_state"][field]) - 90 - (400 if field != "ledger_cash" else 0)
        )
    account["cash_state"]["frozen_cash"] = str(Decimal(account["cash_state"]["frozen_cash"]) + 400)
    evidence = position_evidence("synthetic-second-plan-order", cutoff_at=cutoff)
    for field in (
        "business_effective_at",
        "source_observed_at",
        "locally_acquired_at",
        "validated_at",
    ):
        evidence[field] = cutoff
    account["open_orders"].append(
        {
            "order_id": "synthetic-second-plan-order",
            "security_id": reserve.security_id,
            "side": "BUY",
            "remaining_quantity": "40",
            "reserved_cash": "400",
            "reserved_cash_semantics": "BROKER_FINAL_RESERVED_CASH",
            "evidence": deepcopy(evidence),
        }
    )
    proof = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "second-plan-proof", snapshot),
        clock=GovernanceClock(cutoff),
    ).report
    assert (
        proof is not None
        and proof.result.position is not None
        and proof.result.position.disposition == "RECONCILED"
    ), proof
    for order in command["orders"]:
        order["evidence"] = deepcopy(evidence)
    second_order = deepcopy(command["orders"][0])
    second_order.update(
        order_id="synthetic-second-plan-order",
        quantity="50",
        filled_quantity="10",
        remaining_quantity="40",
        reserved_cash="400",
        status="OPEN",
        submitted_at="2042-05-19T16:02:40Z",
        causal_reservation_id=reserve.commitment_id,
        step_confirmation_id=second_confirmation.event_id,
    )
    command["orders"].append(second_order)
    command["fills"].append(
        {
            "entry_id": entry["entry_id"],
            "order_id": second_order["order_id"],
            "account_id": reserve.account_id,
            "price": "9",
            "fee_entry_ids": [],
        }
    )
    payload["knowledge_cutoff"] = cutoff
    command.update(
        idempotency_key="old-plan-forward",
        seen_execution_id=first.event_id,
        cutoff_at=cutoff,
        position_event_id=proof.event_id,
        broker_evidence=deepcopy(evidence),
        funds_evidence=deepcopy(evidence),
        withdrawn_reservation_ids=[],
    )
    freeze_execution(payload)
    old_plan_result = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(cutoff)
    ).report
    assert old_plan_result is not None and old_plan_result.result.candidate_execution is not None
    assert old_plan_result.result.candidate_execution.disposition == "RECONCILED"
    assert [r.reservation_id for r in old_plan_result.result.candidate_execution.fills] == [
        first.result.candidate_execution.fills[0].reservation_id,
        reserve.commitment_id,
    ]
    assert old_plan_result.result.candidate_execution.reservations == ()
    new_request = deepcopy(payload)
    new_request["business_identity"] = "synthetic-execution:second-plan"
    new_request["candidate_execution"].update(
        plan_event_id=second_plan.event_id,
        confirmation_event_id=second_confirmation.event_id,
        idempotency_key="second-plan-execution",
        seen_execution_id=None,
    )
    freeze_execution(new_request)
    new_result = run_frozen_decision_case(
        migrated_settings, new_request, clock=GovernanceClock(cutoff)
    ).report
    assert new_result is not None and new_result.result.candidate_execution is not None
    assert new_result.result.candidate_execution.disposition == "RECONCILED", (
        new_result.result.candidate_execution.reasons
    )
    assert new_result.result.candidate_execution.reservations[0].principal == 400
    assert new_result.result.candidate_execution.rows[0].filled_quantity == 10
    assert new_result.result.candidate_execution.unassociated_commitments == ()
    newest = forward_allocation_payload(migrated_settings, new_request)
    next_report = run_frozen_decision_case(
        migrated_settings, newest, clock=GovernanceClock(cutoff)
    ).report
    assert next_report is not None and next_report.result.candidate_allocation is not None
    assert next_report.result.candidate_allocation.disposition == "PLANNED", (
        next_report.result.candidate_allocation.reasons
    )
    assert len(next_report.result.candidate_allocation.commitments) == 2
    assert next_report.result.candidate_allocation.rows[0].committed_exposure == 670


def test_account_deviation_keeps_one_global_claim_and_qualified_order_identity(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    payload, _ = broker_payload(migrated_settings, broker_account_id="synthetic-account-8029")
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    outcome = report.result.candidate_execution
    assert outcome.fills[0].classification == "DEVIATION"
    assert "ACCOUNT_DEVIATION" in outcome.fills[0].reasons
    assert outcome.reservations[0].principal == 500
    later = forward_allocation_payload(migrated_settings, payload)
    next_report = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert next_report is not None and next_report.result.candidate_allocation is not None
    allocated = next_report.result.candidate_allocation
    assert allocated.disposition == "PLANNED", allocated.reasons
    assert allocated.rows[0].committed_exposure == 680
    assert len(allocated.commitments) == 2
    assert allocated.commitments[0].broker_order_bindings == (
        ("synthetic-account-8029", "synthetic-execution-order"),
    )
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        liquidity = ledger.get_formal_report_for_event(
            later["candidate_allocation"]["risk_handoff"]["liquidity_event_id"], connection
        )
    assert liquidity is not None and liquidity.result.liquidity is not None
    assert liquidity.result.liquidity.deployable_purchase_cash is not None
    assert allocated.remaining_cash == liquidity.result.liquidity.deployable_purchase_cash - 700


@pytest.mark.parametrize("unknown", [False, True])
def test_unfinished_order_cannot_disappear_without_authoritative_terminal(
    migrated_settings: Settings, unknown: bool
) -> None:
    from test_position_state_reconciliation import position_case_payload

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    payload, accepted = broker_payload(migrated_settings, quantity="0")
    command = payload["candidate_execution"]
    command["fills"] = []
    if unknown:
        command["orders"][0]["status"] = "UNKNOWN"
    original_order = deepcopy(command["orders"][0])
    freeze_execution(payload)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert first is not None and first.result.candidate_execution is not None
    command = payload["candidate_execution"]
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        proof = ledger.get_decision_event(command["position_event_id"], connection)
    assert proof is not None and proof.case.position is not None
    snapshot = proof.case.position.model_dump(mode="json")
    snapshot["snapshot_id"] = "synthetic-disappeared-order"
    for account in snapshot["accounts"]:
        if account["account_id"] == command["orders"][0]["account_id"]:
            account["open_orders"] = []
            account["cash_state"]["frozen_cash"] = "0"
            for field in ("trading_cash", "transferable_cash"):
                account["cash_state"][field] = str(Decimal(account["cash_state"][field]) + 700)
    current = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "disappeared-order", snapshot),
        clock=GovernanceClock(payload["knowledge_cutoff"]),
    ).report
    assert current is not None and current.result.position is not None
    assert current.result.position.disposition == "RECONCILED"
    command.update(
        position_event_id=current.event_id,
        orders=[],
        seen_execution_id=first.event_id,
        idempotency_key="disappeared-order",
        withdrawn_reservation_ids=[
            r.commitment_id for r in accepted.result.candidate_confirmation.reservations
        ],
    )
    freeze_execution(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_execution is not None
    assert report.result.candidate_execution.disposition == "BLOCKED"
    assert (
        report.result.candidate_execution.reservations
        == first.result.candidate_execution.reservations
    )
    assert report.result.candidate_execution.terminal_outcome is None
    command = payload["candidate_execution"]
    original_order.update(status="REJECTED", remaining_quantity="0", reserved_cash="0")
    command.update(
        orders=[original_order],
        seen_execution_id=report.event_id,
        idempotency_key="authoritative-order-terminal",
    )
    freeze_execution(payload)
    closed = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert closed is not None and closed.result.candidate_execution is not None
    assert closed.result.candidate_execution.disposition == "RECONCILED"
    assert closed.result.candidate_execution.reservations == ()
    assert closed.result.candidate_execution.terminal_outcome == "NO_TRADE"
