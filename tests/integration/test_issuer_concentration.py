from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_portfolio_authorization import (
    portfolio_case_payload,
    portfolio_confirmation_command,
    portfolio_fixture,
    portfolio_proposal,
    snapshot_at,
)
from test_position_state_reconciliation import (
    position_case_payload,
    position_evidence,
    position_snapshot_command,
    refresh_current_position_evidence,
)
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.delivery.access import AccessPrincipal


def concentration_authorization(settings: Settings) -> str:
    execution = run_frozen_decision_case(
        settings, concentration_authorization_payload(settings), clock=GovernanceClock()
    )
    assert execution.report is not None
    portfolio = execution.report.result.portfolio
    assert portfolio is not None and portfolio.authorization is not None
    return portfolio.authorization.authorization_id


def concentration_authorization_payload(settings: Settings) -> dict[str, Any]:
    proposal = portfolio_proposal()
    second = deepcopy(portfolio_fixture()["next_cash_account"])
    second["captured_at"] = proposal["snapshot"]["cutoff_at"]
    proposal["snapshot"]["accounts"] = [proposal["snapshot"]["accounts"][0], second]
    proposal["snapshot"]["selected_account_ids"] = [
        "synthetic-account-4017",
        "synthetic-account-8029",
    ]
    proposal["cash_obligations"] = []
    return portfolio_case_payload(
        settings, "concentration-authorization", portfolio_confirmation_command(proposal)
    )


def concentration_payload(
    settings: Settings, authorization_id: str, *, quantity: str = "30", identity: str = "normal"
) -> dict[str, Any]:
    snapshot = position_snapshot_command()
    snapshot["generator_version"] = "issuer-concentration-fixture-v1"
    snapshot["seed"] = 7307
    snapshot["annotations"] = []
    remaining_cash = Decimal("10000") - (Decimal("100") + Decimal(quantity)) * Decimal("10")
    for account, count, cash in zip(
        snapshot["accounts"], ("100", quantity), (str(remaining_cash), "0"), strict=True
    ):
        position = account["positions"][0]
        cost = Decimal(count) * Decimal("9")
        position.update(
            total_quantity=count,
            broker_sellable_quantity=count,
            unsettled_quantity="0",
            frozen_quantity="0",
            restricted_quantity="0",
            open_sell_order_quantity="0",
            reported_cost_basis=str(cost),
            market_price="10",
        )
        account["open_orders"] = []
        account["ledger_entries"] = [account["ledger_entries"][0]]
        account["ledger_entries"][0].update(
            quantity_delta=count, cost_basis_delta=str(cost), cash_delta=str(-cost)
        )
        account["cash_state"].update(
            opening_ledger_cash=str(Decimal(cash) + cost),
            ledger_cash=cash,
            trading_cash=cash,
            transferable_cash=cash,
            frozen_cash="0",
            receivable_cash="0",
            payable_cash="0",
        )
        account["account_equity"] = str(Decimal(cash) + Decimal(count) * Decimal("10"))
    payload = position_case_payload(settings, identity, snapshot)
    del payload["position"]
    payload["version_bundle"].update(
        case_contract_version="8.1.0",
        host_contract_version="8.1.0",
        report_projection_contract_version="8.1.0",
    )
    payload["concentration"] = {
        "operation": "ISSUER_CONCENTRATION_ASSESS",
        "contract_version": "1.0.0",
        "authorization": {
            "operation": "PORTFOLIO_USE",
            "portfolio_id": "synthetic-decision-portfolio-alpha",
            "authorization_id": authorization_id,
            "requested_action": "DETERMINISTIC_PROTECTION",
        },
        "position_snapshot": snapshot,
        "liquidation_costs": [
            {
                "account_id": account["account_id"],
                "amount": "0",
                "evidence": deepcopy(account["account_equity_evidence"]),
            }
            for account in snapshot["accounts"]
        ],
    }
    return payload


@pytest.mark.parametrize("other_version", ["8.0.0", "8.2.0"])
def test_concentration_contract_does_not_redefine_released_risk_versions(
    migrated_settings: Settings,
    other_version: str,
) -> None:
    payload = concentration_payload(migrated_settings, "synthetic-authorization")
    assert FrozenDecisionCase.model_validate(payload).concentration is not None
    payload["version_bundle"].update(
        case_contract_version=other_version,
        host_contract_version=other_version,
        report_projection_contract_version=other_version,
    )
    with pytest.raises(ValidationError, match="concentration requires the version 8.1"):
        FrozenDecisionCase.model_validate(payload)


def test_target_boundary_preserves_security_result_and_committed_report(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id)

    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())

    report = execution.report
    assert report is not None
    outcome = report.result.concentration
    assert outcome is not None
    assert outcome.disposition == "ASSESSED"
    assert outcome.portfolio_net_liquidation_equity == Decimal("10000")
    assert outcome.risk_budget_version_id == "synthetic-risk-budget-alpha"
    issuer = outcome.issuers[0]
    assert issuer.current_market_exposure == Decimal("1300")
    assert issuer.position_weight == Decimal("0.13")
    assert issuer.state == "NORMAL"
    assert issuer.new_exposure_blocked is False
    assert issuer.direction is None
    assert issuer.obligation_id is None
    assert issuer.targets == ()
    assert report.result.outcome_code == payload["expected_external_result"]["outcome_code"]
    assert (
        get_formal_report(
            report.report_version_id,
            migrated_settings,
            principal=AccessPrincipal(
                user_id="stock-profiler-single-user",
                account_ids=("synthetic-account-4017", "synthetic-account-8029"),
                permissions=("REPORT_READ",),
            ),
        )
        == report
    )
    assert (
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
        == report
    )


@pytest.mark.parametrize(
    ("quantity", "weight", "state", "direction", "gap"),
    [
        ("30.001", "0.130001", "BUFFER", None, "0"),
        ("90", "0.19", "BUFFER", None, "0"),
        ("90.001", "0.190001", "REMEDIATION_REQUIRED", "REDUCE", "600.01"),
    ],
)
def test_buffer_and_hard_boundaries_have_distinct_actions(
    migrated_settings: Settings,
    quantity: str,
    weight: str,
    state: str,
    direction: str | None,
    gap: str,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    execution = run_frozen_decision_case(
        migrated_settings,
        concentration_payload(migrated_settings, authorization_id, quantity=quantity),
        clock=GovernanceClock(),
    )
    assert execution.report is not None
    outcome = execution.report.result.concentration
    assert outcome is not None and outcome.disposition == "ASSESSED"
    issuer = outcome.issuers[0]
    assert issuer.position_weight == Decimal(weight)
    assert issuer.state == state
    assert issuer.new_exposure_blocked is True
    assert issuer.direction == direction
    assert issuer.exposure_gap == Decimal(gap)
    if direction is None:
        assert issuer.targets == ()
        assert issuer.obligation_id is None
    else:
        assert issuer.obligation_id
        assert issuer.targets[0].security_id == "XQZ-4017"
        assert issuer.targets[0].target_quantity == Decimal("130")
        assert issuer.targets[0].required_reduction_quantity == Decimal("60.001")
        assert issuer.execution_blocked is False


def later_concentration_payload(payload: dict[str, Any], day: str) -> dict[str, Any]:
    later = deepcopy(payload)
    later["business_identity"] += f":{day}"
    later["case_id"] += f"-{day}"
    later["knowledge_cutoff"] = f"2042-05-{day}T16:00:00Z"
    command = later["concentration"]
    snapshot = command["position_snapshot"]
    snapshot["snapshot_id"] += f"-{day}"
    snapshot["cutoff_at"] = later["knowledge_cutoff"]
    refresh_current_position_evidence(snapshot, snapshot["cutoff_at"])
    for cost in command["liquidation_costs"]:
        cost["evidence"] = position_evidence(
            cost["evidence"]["source"], cutoff_at=snapshot["cutoff_at"]
        )
    return later


def test_price_change_preserves_the_original_unexecuted_target_and_identity(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None and first.report.result.concentration is not None
    original = first.report.result.concentration.issuers[0]
    assert original.targets[0].target_quantity == Decimal("130")
    later = later_concentration_payload(payload, "18")
    for account in later["concentration"]["position_snapshot"]["accounts"]:
        account["positions"][0]["market_price"] = "5"
        account["account_equity"] = str(Decimal(account["account_equity"]) - Decimal("500"))

    second = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-18T17:00:00+00:00")
    )

    assert second.report is not None and second.report.result.concentration is not None
    current = second.report.result.concentration.issuers[0]
    assert current.position_weight is not None
    assert current.position_weight < Decimal("0.13")
    assert current.state == "REMEDIATION_REQUIRED"
    assert current.direction == "REDUCE"
    assert current.obligation_id == original.obligation_id
    assert current.targets[0].target_quantity == Decimal("130")
    assert current.targets[0].required_reduction_quantity == Decimal("70")
    assert current.new_exposure_blocked is True
    assert current.exposure_gap == Decimal("350")


def test_missing_current_price_retains_reduction_with_unknown_execution_gap(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None and first.report.result.concentration is not None
    original = first.report.result.concentration.issuers[0]
    later = later_concentration_payload(payload, "18")
    later["concentration"]["position_snapshot"]["accounts"][0]["positions"][0]["market_price"] = (
        None
    )

    second = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-18T17:00:00+00:00")
    )

    assert second.report is not None and second.report.result.concentration is not None
    outcome = second.report.result.concentration
    assert outcome.disposition == "BLOCKED"
    current = outcome.issuers[0]
    assert current.direction == "REDUCE"
    assert current.obligation_id == original.obligation_id
    assert current.execution_blocked is True
    assert current.new_exposure_blocked is True
    assert current.exposure_gap is None
    assert current.targets[0].target_quantity == Decimal("130")
    assert current.targets[0].required_reduction_quantity is None


def record_synthetic_sale(
    payload: dict[str, Any], quantity: str, identity: str, *, account_index: int = 0
) -> None:
    snapshot = payload["concentration"]["position_snapshot"]
    account = snapshot["accounts"][account_index]
    position = account["positions"][0]
    sold = Decimal(quantity)
    position["total_quantity"] = str(Decimal(position["total_quantity"]) - sold)
    position["broker_sellable_quantity"] = position["total_quantity"]
    position["reported_cost_basis"] = str(Decimal(position["reported_cost_basis"]) - sold * 9)
    position["open_sell_order_quantity"] = "0"
    account["open_orders"] = []
    for field in ("ledger_cash", "trading_cash", "transferable_cash"):
        account["cash_state"][field] = str(Decimal(account["cash_state"][field]) + sold * 10)
    account["ledger_entries"].append(
        {
            "entry_id": f"synthetic-concentration-sale-{identity}",
            "entry_type": "FILL",
            "security_id": position["security_id"],
            "quantity_delta": str(-sold),
            "cost_basis_delta": str(-sold * 9),
            "cash_delta": str(sold * 10),
            "occurred_at": snapshot["cutoff_at"].replace("16:00:00", "15:00:00"),
            "evidence": position_evidence(
                f"synthetic-sale-{identity}", cutoff_at=snapshot["cutoff_at"]
            ),
        }
    )


def test_pending_sales_do_not_release_and_only_reconciled_fills_complete_the_obligation(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None and first.report.result.concentration is not None
    original = first.report.result.concentration.issuers[0]
    pending = later_concentration_payload(payload, "18")
    account = pending["concentration"]["position_snapshot"]["accounts"][0]
    account["positions"][0].update(open_sell_order_quantity="70", broker_sellable_quantity="30")
    account["open_orders"] = [
        {
            "order_id": "synthetic-concentration-pending-sale",
            "security_id": "XQZ-4017",
            "side": "SELL",
            "remaining_quantity": "70",
            "reserved_cash": None,
            "reserved_cash_semantics": "NOT_APPLICABLE",
            "evidence": position_evidence(
                "synthetic-pending-sale", cutoff_at=pending["knowledge_cutoff"]
            ),
        }
    ]
    submitted = run_frozen_decision_case(
        migrated_settings, pending, clock=GovernanceClock("2042-05-18T17:00:00+00:00")
    )
    assert submitted.report is not None and submitted.report.result.concentration is not None
    submitted_issuer = submitted.report.result.concentration.issuers[0]
    assert submitted_issuer.current_market_exposure == Decimal("2000")
    assert submitted_issuer.exposure_gap == Decimal("700")
    assert submitted_issuer.obligation_id == original.obligation_id

    partial = later_concentration_payload(pending, "19")
    record_synthetic_sale(partial, "20", "partial")
    partial_execution = run_frozen_decision_case(
        migrated_settings, partial, clock=GovernanceClock("2042-05-19T17:00:00+00:00")
    )
    assert partial_execution.report is not None
    partial_outcome = partial_execution.report.result.concentration
    assert partial_outcome is not None
    remaining = partial_outcome.issuers[0]
    assert remaining.obligation_id == original.obligation_id
    assert remaining.targets[0].target_quantity == Decimal("130")
    assert remaining.targets[0].required_reduction_quantity == Decimal("50")
    assert remaining.exposure_gap == Decimal("500")

    completed = later_concentration_payload(partial, "20")
    record_synthetic_sale(completed, "50", "complete")
    final = run_frozen_decision_case(
        migrated_settings, completed, clock=GovernanceClock("2042-05-20T17:00:00+00:00")
    )
    assert final.report is not None and final.report.result.concentration is not None
    resolved = final.report.result.concentration.issuers[0]
    assert resolved.obligation_id == original.obligation_id
    assert resolved.state == "RESOLVED"
    assert resolved.direction is None
    assert resolved.new_exposure_blocked is False
    assert resolved.exposure_gap == Decimal("0")
    assert resolved.targets[0].target_quantity == Decimal("130")


@pytest.mark.parametrize("restriction", ["SUSPENDED", "INSUFFICIENT_SELLABLE"])
def test_unavailable_execution_preserves_direction_target_and_gap(
    migrated_settings: Settings, restriction: str
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    snapshot = payload["concentration"]["position_snapshot"]
    for account in snapshot["accounts"]:
        if restriction == "SUSPENDED":
            account["execution_restrictions"] = [
                {
                    "restriction_id": f"synthetic-suspension-{account['account_id']}",
                    "security_id": "XQZ-4017",
                    "kind": "SUSPENDED",
                    "reason": "Synthetic frozen suspension",
                    "active": True,
                    "evidence": position_evidence("synthetic-suspension"),
                }
            ]
        else:
            account["positions"][0].update(broker_sellable_quantity="10", frozen_quantity="90")
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None and execution.report.result.concentration is not None
    issuer = execution.report.result.concentration.issuers[0]
    assert issuer.direction == "REDUCE"
    assert issuer.targets[0].target_quantity == Decimal("130")
    assert issuer.obligation_risk_budget_version_id == "synthetic-risk-budget-alpha"
    assert issuer.exposure_gap == Decimal("700")
    assert issuer.execution_blocked is True


def test_liquidation_costs_reduce_the_denominator_not_the_issuer_exposure(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="90")
    payload["concentration"]["liquidation_costs"][0]["amount"] = "500"
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None and execution.report.result.concentration is not None
    outcome = execution.report.result.concentration
    assert outcome.portfolio_net_liquidation_equity == Decimal("9500")
    assert outcome.issuers[0].current_market_exposure == Decimal("1900")
    assert outcome.issuers[0].position_weight == Decimal("0.2")
    assert outcome.issuers[0].targets[0].target_quantity == Decimal("123.5")


def test_relabeling_an_issuer_cannot_remove_its_pending_obligation(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None and first.report.result.concentration is not None
    original = first.report.result.concentration.issuers[0]
    later = later_concentration_payload(payload, "18")
    for account in later["concentration"]["position_snapshot"]["accounts"]:
        account["positions"][0]["issuer_id"] = "FICTIONAL-OTHER-ISSUER"
    second = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-18T17:00:00+00:00")
    )
    assert second.report is not None and second.report.result.concentration is not None
    outcome = second.report.result.concentration
    assert outcome.disposition == "BLOCKED"
    retained = next(
        issuer for issuer in outcome.issuers if issuer.obligation_id == original.obligation_id
    )
    assert retained.direction == "REDUCE"
    assert retained.execution_blocked is True
    assert retained.targets[0].target_quantity == Decimal("130")


def test_superseded_authorization_cannot_bypass_a_tighter_effective_policy(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    proposal = portfolio_proposal()
    second = deepcopy(portfolio_fixture()["next_cash_account"])
    second["captured_at"] = proposal["snapshot"]["cutoff_at"]
    proposal["snapshot"]["accounts"] = [proposal["snapshot"]["accounts"][0], second]
    proposal["snapshot"]["selected_account_ids"] = [
        "synthetic-account-4017",
        "synthetic-account-8029",
    ]
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-tightening-selection",
        cutoff="2042-05-18T16:00:00Z",
    )
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-tightening-activation",
        cutoff="2042-05-19T16:00:00Z",
    )
    proposal["risk_budget"].update(
        version_id="synthetic-tighter-risk-budget",
        effective_at="2042-05-19T16:00:00Z",
        expires_at="2042-11-19T16:00:00Z",
        concentration={"target_ratio": "0.11", "hard_ratio": "0.17"},
    )
    proposal["cash_obligations"] = []
    confirmed = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "concentration-tightening",
            portfolio_confirmation_command(
                proposal,
                previous_authorization_id=authorization_id,
                confirmed_at="2042-05-18T17:00:00Z",
            ),
        ),
        clock=GovernanceClock("2042-05-19T17:00:00+00:00"),
    )
    assert confirmed.report is not None and confirmed.report.result.portfolio is not None
    assert confirmed.report.result.portfolio.disposition == "APPROVED"
    payload = later_concentration_payload(
        concentration_payload(migrated_settings, authorization_id, quantity="80"), "20"
    )
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-20T17:00:00+00:00")
    )
    assert execution.report is not None and execution.report.result.concentration is not None
    outcome = execution.report.result.concentration
    assert outcome.disposition == "BLOCKED"
    assert "CONCENTRATION_CURRENT_AUTHORIZATION_REQUIRED" in outcome.reasons
    assert outcome.issuers[0].new_exposure_blocked is True


def test_late_old_snapshot_cannot_replace_newer_obligation_history(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    newer = later_concentration_payload(payload, "19")
    first = run_frozen_decision_case(
        migrated_settings, newer, clock=GovernanceClock("2042-05-19T17:00:00+00:00")
    )
    assert first.report is not None and first.report.result.concentration is not None
    obligation_id = first.report.result.concentration.issuers[0].obligation_id
    late = later_concentration_payload(payload, "18")
    late_result = run_frozen_decision_case(
        migrated_settings, late, clock=GovernanceClock("2042-05-19T18:00:00+00:00")
    )
    assert late_result.report is not None and late_result.report.result.concentration is not None
    assert late_result.report.result.concentration.disposition == "BLOCKED"
    resumed = later_concentration_payload(newer, "20")
    for account in resumed["concentration"]["position_snapshot"]["accounts"]:
        account["positions"][0]["market_price"] = "5"
        account["account_equity"] = str(Decimal(account["account_equity"]) - 500)
    final = run_frozen_decision_case(
        migrated_settings, resumed, clock=GovernanceClock("2042-05-20T17:00:00+00:00")
    )
    assert final.report is not None and final.report.result.concentration is not None
    issuer = final.report.result.concentration.issuers[0]
    assert issuer.obligation_id == obligation_id
    assert issuer.targets[0].target_quantity == Decimal("130")
    assert issuer.direction == "REDUCE"


def transfer_to_admitted_account(payload: dict[str, Any]) -> None:
    accounts = payload["concentration"]["position_snapshot"]["accounts"]
    transferred_position = deepcopy(accounts[0]["positions"][0])
    transferred_position["position_id"] = "synthetic-admitted-transfer-position"
    transferred_position["lifecycle_id"] = "synthetic-admitted-transfer-lifecycle"
    accounts[2]["positions"] = [transferred_position]
    accounts[2]["account_equity"] = "11000"
    accounts[0]["positions"][0].update(
        total_quantity="0", broker_sellable_quantity="0", reported_cost_basis="0"
    )
    accounts[0]["account_equity"] = "8000"
    for index, quantity, cost in ((0, "-100", "-900"), (2, "100", "900")):
        accounts[index]["ledger_entries"].append(
            {
                "entry_id": f"synthetic-admitted-transfer-{index}",
                "entry_type": "TRANSFER_OUT" if index == 0 else "TRANSFER_IN",
                "security_id": "XQZ-4017",
                "quantity_delta": quantity,
                "cost_basis_delta": cost,
                "cash_delta": "0",
                "occurred_at": payload["knowledge_cutoff"].replace("16:00:00", "15:00:00"),
                "evidence": position_evidence(
                    "synthetic-admitted-transfer", cutoff_at=payload["knowledge_cutoff"]
                ),
            }
        )


@pytest.mark.parametrize("admission_scenario", ["empty", "prior-sale", "transfer"])
def test_authorized_account_addition_cannot_reset_an_unexecuted_obligation(
    migrated_settings: Settings,
    admission_scenario: str,
) -> None:
    pre_admission_sale = admission_scenario == "prior-sale"
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    initial = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert initial.report is not None and initial.report.result.concentration is not None
    original = initial.report.result.concentration.issuers[0]
    assert initial.report.result.portfolio is not None
    usage = initial.report.result.portfolio.usage
    assert usage is not None
    proposal = usage.authorization_snapshot.proposal.model_dump(mode="json")
    third = deepcopy(proposal["snapshot"]["accounts"][0])
    third["account_id"] = "synthetic-account-9031"
    for field in (
        "cash_fact_id",
        "positions_fact_id",
        "receivables_fact_id",
        "payables_fact_id",
        "unfinished_trades_fact_id",
    ):
        third[field] = f"synthetic-9031-{field}"
    proposal["snapshot"]["accounts"].append(third)
    proposal["snapshot"]["selected_account_ids"].append(third["account_id"])
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-account-addition-selection",
        cutoff="2042-05-18T16:00:00Z",
    )
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-account-addition-activation",
        cutoff="2042-05-19T16:00:00Z",
    )
    proposal["risk_budget"].update(
        version_id="synthetic-expanded-risk-budget",
        effective_at="2042-05-19T16:00:00Z",
        expires_at="2042-11-19T16:00:00Z",
    )
    confirmed = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "concentration-account-addition",
            portfolio_confirmation_command(
                proposal,
                previous_authorization_id=authorization_id,
                confirmed_at="2042-05-18T17:00:00Z",
            ),
        ),
        clock=GovernanceClock("2042-05-19T17:00:00+00:00"),
    )
    assert confirmed.report is not None and confirmed.report.result.portfolio is not None
    successor = confirmed.report.result.portfolio.authorization
    assert successor is not None
    later = later_concentration_payload(payload, "20")
    command = later["concentration"]
    command["authorization"]["authorization_id"] = successor.authorization_id
    later["access_scope"]["account_ids"].append(third["account_id"])
    account = deepcopy(command["position_snapshot"]["accounts"][1])
    account.update(
        account_id=third["account_id"],
        account_equity="10000",
        positions=[],
        open_orders=[],
        ledger_entries=[],
        execution_restrictions=[],
    )
    account["cash_state"].update(
        opening_ledger_cash="10000",
        ledger_cash="10000",
        trading_cash="10000",
        transferable_cash="10000",
    )
    command["position_snapshot"]["accounts"].append(account)
    command["liquidation_costs"].append(
        {
            "account_id": third["account_id"],
            "amount": "0",
            "evidence": deepcopy(account["account_equity_evidence"]),
        }
    )
    if pre_admission_sale:
        bought = deepcopy(command["position_snapshot"]["accounts"][1]["ledger_entries"][0])
        bought["entry_id"] = "synthetic-pre-admission-buy"
        sold = deepcopy(bought)
        sold.update(
            entry_id="synthetic-pre-admission-sale",
            quantity_delta="-100",
            cost_basis_delta="-900",
            cash_delta="1000",
            occurred_at="2042-05-18T15:00:00Z",
            evidence=position_evidence(
                "synthetic-pre-admission-sale", cutoff_at="2042-05-18T16:00:00Z"
            ),
        )
        account["ledger_entries"] = [bought, sold]
        account["cash_state"]["opening_ledger_cash"] = "9900"
        first_account = command["position_snapshot"]["accounts"][0]
        first_account["positions"][0].update(
            total_quantity="0", broker_sellable_quantity="0", reported_cost_basis="0"
        )
        first_account["account_equity"] = "8000"
        transferred = deepcopy(first_account["ledger_entries"][0])
        transferred.update(
            entry_id="synthetic-post-admission-transfer",
            entry_type="TRANSFER_OUT",
            quantity_delta="-100",
            cost_basis_delta="-900",
            cash_delta="0",
            occurred_at="2042-05-20T15:00:00Z",
            evidence=position_evidence(
                "synthetic-post-admission-transfer", cutoff_at=later["knowledge_cutoff"]
            ),
        )
        first_account["ledger_entries"].append(transferred)
    if admission_scenario == "transfer":
        transfer_to_admitted_account(later)
    execution = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-20T17:00:00+00:00")
    )
    assert execution.report is not None and execution.report.result.concentration is not None
    outcome = execution.report.result.concentration
    if not pre_admission_sale:
        assert outcome.disposition == "ASSESSED"
        assert outcome.portfolio_net_liquidation_equity == Decimal("20000")
    issuer = outcome.issuers[0]
    if not pre_admission_sale:
        assert issuer.position_weight == Decimal("0.1")
    assert issuer.direction == "REDUCE"
    assert issuer.obligation_id == original.obligation_id
    assert issuer.targets[0].target_quantity == Decimal("130")
    if not pre_admission_sale:
        moved = later_concentration_payload(later, "21")
        if admission_scenario == "empty":
            transfer_to_admitted_account(moved)
        transfer_result = run_frozen_decision_case(
            migrated_settings, moved, clock=GovernanceClock("2042-05-21T17:00:00+00:00")
        )
        assert transfer_result.report is not None
        assert transfer_result.report.result.concentration is not None
        assert transfer_result.report.result.concentration.issuers[0].exposure_gap == 700
        sold_after_admission = later_concentration_payload(moved, "22")
        record_synthetic_sale(sold_after_admission, "70", "admitted-sale", account_index=2)
        completed = run_frozen_decision_case(
            migrated_settings,
            sold_after_admission,
            clock=GovernanceClock("2042-05-22T17:00:00+00:00"),
        )
        assert completed.report is not None and completed.report.result.concentration is not None
        assert completed.report.result.concentration.issuers[0].state == "RESOLVED"


def test_quantity_changing_corporate_action_cannot_complete_a_sale_obligation(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None and first.report.result.concentration is not None
    original = first.report.result.concentration.issuers[0]
    later = later_concentration_payload(payload, "18")
    for account in later["concentration"]["position_snapshot"]["accounts"]:
        position = account["positions"][0]
        position.update(total_quantity="50", broker_sellable_quantity="50")
        account["account_equity"] = str(Decimal(account["account_equity"]) - 500)
        account["ledger_entries"].append(
            {
                "entry_id": f"synthetic-share-consolidation-{account['account_id']}",
                "entry_type": "CORPORATE_ACTION",
                "security_id": position["security_id"],
                "quantity_delta": "-50",
                "cost_basis_delta": "0",
                "cash_delta": "0",
                "occurred_at": "2042-05-18T15:00:00Z",
                "evidence": position_evidence(
                    "synthetic-share-consolidation", cutoff_at=later["knowledge_cutoff"]
                ),
            }
        )
    execution = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-18T17:00:00+00:00")
    )
    assert execution.report is not None and execution.report.result.concentration is not None
    current = execution.report.result.concentration.issuers[0]
    assert current.obligation_id == original.obligation_id
    assert current.direction == "REDUCE"
    assert current.execution_blocked is True
    assert current.exposure_gap is None
    assert current.targets[0].target_quantity == Decimal("130")


def test_account_removal_requires_obligation_reconciliation_without_disclosing_old_facts(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None and first.report.result.portfolio is not None
    usage = first.report.result.portfolio.usage
    assert usage is not None
    proposal = usage.authorization_snapshot.proposal.model_dump(mode="json")
    proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    proposal["snapshot"]["selected_account_ids"] = ["synthetic-account-4017"]
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-removal-selection",
        cutoff="2042-05-18T16:00:00Z",
    )
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-removal-activation",
        cutoff="2042-05-19T16:00:00Z",
    )
    proposal["risk_budget"].update(
        version_id="synthetic-reduced-account-budget",
        effective_at="2042-05-19T16:00:00Z",
        expires_at="2042-11-19T16:00:00Z",
    )
    confirmed = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "concentration-removal",
            portfolio_confirmation_command(
                proposal,
                previous_authorization_id=authorization_id,
                confirmed_at="2042-05-18T17:00:00Z",
            ),
        ),
        clock=GovernanceClock("2042-05-19T17:00:00+00:00"),
    )
    assert confirmed.report is not None and confirmed.report.result.portfolio is not None
    successor = confirmed.report.result.portfolio.authorization
    assert successor is not None
    later = later_concentration_payload(payload, "20")
    later["access_scope"]["account_ids"] = ["synthetic-account-4017"]
    command = later["concentration"]
    command["authorization"]["authorization_id"] = successor.authorization_id
    command["position_snapshot"]["accounts"] = command["position_snapshot"]["accounts"][:1]
    command["liquidation_costs"] = command["liquidation_costs"][:1]
    execution = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-20T17:00:00+00:00")
    )
    assert execution.report is not None and execution.report.result.concentration is not None
    outcome = execution.report.result.concentration
    assert outcome.disposition == "BLOCKED"
    assert "CONCENTRATION_HISTORY_SCOPE_UNRESOLVED" in outcome.reasons
    assert outcome.issuers[0].new_exposure_blocked is True
    assert outcome.issuers[0].obligation_id is None
    assert outcome.issuers[0].targets == ()
    restored = later_concentration_payload(payload, "21")
    recovered = run_frozen_decision_case(
        migrated_settings, restored, clock=GovernanceClock("2042-05-21T17:00:00+00:00")
    )
    assert recovered.report is not None and recovered.report.result.concentration is not None
    retained = recovered.report.result.concentration.issuers[0]
    assert retained.direction == "REDUCE"
    assert retained.targets[0].target_quantity == 130


@pytest.mark.parametrize("result", ["ABSTAINED", "FAILED", "UNKNOWN"])
def test_business_result_cannot_cancel_deterministic_protection(
    migrated_settings: Settings,
    result: str,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    code = f"SYNTHETIC_RESULT_{result}"
    payload["input"]["scenario"] = code
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "fixtures"
            / "synthetic"
            / "result-families"
            / f"result-{result.lower()}.json"
        ).read_text(encoding="utf-8")
    )
    payload["expected_external_result"] = fixture["expected_external_result"]
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None and execution.report.result.concentration is not None
    assert execution.report.result.outcome_code == code
    issuer = execution.report.result.concentration.issuers[0]
    assert issuer.direction == "REDUCE"
    assert issuer.targets[0].target_quantity == Decimal("130")


def test_conflicting_prices_for_one_security_cannot_produce_an_arbitrary_execution_gap(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    account = payload["concentration"]["position_snapshot"]["accounts"][1]
    account["positions"][0]["market_price"] = "20"
    account["account_equity"] = "2000"
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None and execution.report.result.concentration is not None
    outcome = execution.report.result.concentration
    assert outcome.disposition == "BLOCKED"
    assert "CONCENTRATION_SECURITY_PRICE_CONFLICT" in outcome.reasons
    assert outcome.issuers[0].execution_blocked is True


def test_asymmetric_fills_preserve_each_security_cap_within_one_issuer(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    second = payload["concentration"]["position_snapshot"]["accounts"][1]
    second["positions"][0]["security_id"] = "XQZ-SECOND-CLASS"
    second["ledger_entries"][0]["security_id"] = "XQZ-SECOND-CLASS"
    original = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert original.report is not None and original.report.result.concentration is not None
    initial = original.report.result.concentration.issuers[0]
    assert [target.target_quantity for target in initial.targets] == [Decimal("65")] * 2
    partial = later_concentration_payload(payload, "18")
    record_synthetic_sale(partial, "35", "first-class")
    execution = run_frozen_decision_case(
        migrated_settings, partial, clock=GovernanceClock("2042-05-18T17:00:00+00:00")
    )
    assert execution.report is not None and execution.report.result.concentration is not None
    issuer = execution.report.result.concentration.issuers[0]
    assert issuer.obligation_id == initial.obligation_id
    assert [target.target_quantity for target in issuer.targets] == [Decimal("65")] * 2
    assert issuer.targets[0].required_reduction_quantity == 0
    assert issuer.targets[1].required_reduction_quantity == 35
    assert issuer.exposure_gap == 350


def test_security_code_transfer_cannot_reset_the_unexecuted_cap(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    original = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert original.report is not None and original.report.result.concentration is not None
    initial = original.report.result.concentration.issuers[0]
    later = later_concentration_payload(payload, "18")
    for account in later["concentration"]["position_snapshot"]["accounts"]:
        position = account["positions"][0]
        position.update(security_id="XQZ-SUCCESSOR", market_price="5")
        position["position_id"] += "-successor"
        position["lifecycle_id"] += "-successor"
        account["account_equity"] = str(Decimal(account["account_equity"]) - 500)
        for code, quantity, cost in (("XQZ-4017", "-100", "-900"), ("XQZ-SUCCESSOR", "100", "900")):
            account["ledger_entries"].append(
                {
                    "entry_id": f"synthetic-code-transfer-{account['account_id']}-{code}",
                    "entry_type": "TRANSFER_OUT" if Decimal(quantity) < 0 else "TRANSFER_IN",
                    "security_id": code,
                    "quantity_delta": quantity,
                    "cost_basis_delta": cost,
                    "cash_delta": "0",
                    "occurred_at": "2042-05-18T15:00:00Z",
                    "evidence": position_evidence(
                        "synthetic-code-transfer", cutoff_at=later["knowledge_cutoff"]
                    ),
                }
            )
    execution = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-18T17:00:00+00:00")
    )
    assert execution.report is not None and execution.report.result.concentration is not None
    issuer = execution.report.result.concentration.issuers[0]
    assert issuer.obligation_id == initial.obligation_id
    assert issuer.direction == "REDUCE"
    assert issuer.execution_blocked is True
    assert issuer.targets[0].security_id == "XQZ-4017"
    assert issuer.targets[0].target_quantity == 130


@pytest.mark.parametrize("cost_basis", [None, "10"])
def test_cost_basis_defects_do_not_suppress_a_provable_hard_breach(
    migrated_settings: Settings,
    cost_basis: str | None,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    payload["concentration"]["position_snapshot"]["accounts"][0]["positions"][0][
        "reported_cost_basis"
    ] = cost_basis
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None and execution.report.result.concentration is not None
    outcome = execution.report.result.concentration
    assert outcome.portfolio_net_liquidation_equity == 10000
    issuer = outcome.issuers[0]
    assert issuer.current_market_exposure == 2000
    assert issuer.direction == "REDUCE"
    assert issuer.targets[0].target_quantity == 130
    assert issuer.exposure_gap == 700


def test_complete_closing_fills_resolve_obligation_without_zero_position_rows(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None and first.report.result.concentration is not None
    initial = first.report.result.concentration.issuers[0]
    later = later_concentration_payload(payload, "18")
    record_synthetic_sale(later, "100", "close-first")
    record_synthetic_sale(later, "100", "close-second", account_index=1)
    for account in later["concentration"]["position_snapshot"]["accounts"]:
        account["positions"] = []
    execution = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-18T17:00:00+00:00")
    )
    assert execution.report is not None and execution.report.result.concentration is not None
    outcome = execution.report.result.concentration
    assert outcome.disposition == "ASSESSED"
    issuer = outcome.issuers[0]
    assert issuer.obligation_id == initial.obligation_id
    assert issuer.state == "RESOLVED"
    assert issuer.direction is None
    assert issuer.current_market_exposure == 0
    assert issuer.exposure_gap == 0
    assert issuer.new_exposure_blocked is False


@pytest.mark.parametrize("transferred_accounts", [1, 2])
def test_transfers_do_not_discharge_obligation_when_security_rows_remain(
    migrated_settings: Settings,
    transferred_accounts: int,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None and first.report.result.concentration is not None
    original = first.report.result.concentration.issuers[0]
    later = later_concentration_payload(payload, "18")
    for index in range(transferred_accounts):
        record_synthetic_sale(later, "100", f"transfer-{index}", account_index=index)
        account = later["concentration"]["position_snapshot"]["accounts"][index]
        account["ledger_entries"][-1].update(entry_type="TRANSFER_OUT", cash_delta="0")
        for field in ("ledger_cash", "trading_cash", "transferable_cash"):
            account["cash_state"][field] = str(Decimal(account["cash_state"][field]) - 1000)
        account["account_equity"] = str(Decimal(account["account_equity"]) - 1000)
    execution = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-18T17:00:00+00:00")
    )
    assert execution.report is not None and execution.report.result.concentration is not None
    issuer = execution.report.result.concentration.issuers[0]
    assert issuer.obligation_id == original.obligation_id
    assert issuer.direction == "REDUCE"
    assert issuer.new_exposure_blocked is True
    assert issuer.targets[0].target_quantity == 130
    assert issuer.execution_blocked is True


def authorize_changed_scope(
    settings: Settings,
    previous_id: str,
    proposal: dict[str, Any],
    account_ids: list[str],
    selected_day: str,
    effective_day: str,
) -> str:
    proposal = deepcopy(proposal)
    proposal["snapshot"]["accounts"] = [
        account
        for account in proposal["snapshot"]["accounts"]
        if account["account_id"] in account_ids
    ]
    proposal["snapshot"]["selected_account_ids"] = account_ids
    identity = f"synthetic-scope-change-{effective_day}"
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id=f"{identity}-selection",
        cutoff=f"2042-05-{selected_day}T16:00:00Z",
    )
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id=f"{identity}-activation",
        cutoff=f"2042-05-{effective_day}T16:00:00Z",
    )
    proposal["risk_budget"].update(
        version_id=identity,
        effective_at=f"2042-05-{effective_day}T16:00:00Z",
        expires_at=f"2042-11-{effective_day}T16:00:00Z",
    )
    execution = run_frozen_decision_case(
        settings,
        portfolio_case_payload(
            settings,
            identity,
            portfolio_confirmation_command(
                proposal,
                previous_authorization_id=previous_id,
                confirmed_at=f"2042-05-{selected_day}T17:00:00Z",
            ),
        ),
        clock=GovernanceClock(f"2042-05-{effective_day}T17:00:00+00:00"),
    )
    assert execution.report is not None and execution.report.result.portfolio is not None
    authorization = execution.report.result.portfolio.authorization
    assert authorization is not None
    return authorization.authorization_id


def test_scope_denial_cannot_erase_a_previously_admitted_account_baseline(
    migrated_settings: Settings,
) -> None:
    authorization_id = concentration_authorization(migrated_settings)
    payload = concentration_payload(migrated_settings, authorization_id, quantity="100")
    initial = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert initial.report is not None and initial.report.result.portfolio is not None
    usage = initial.report.result.portfolio.usage
    assert usage is not None
    proposal = usage.authorization_snapshot.proposal.model_dump(mode="json")
    third = deepcopy(proposal["snapshot"]["accounts"][1])
    third["account_id"] = "synthetic-account-9031"
    proposal["snapshot"]["accounts"].append(third)
    full_scope = ["synthetic-account-4017", "synthetic-account-8029", "synthetic-account-9031"]
    expanded_id = authorize_changed_scope(
        migrated_settings, authorization_id, proposal, full_scope, "18", "19"
    )
    expanded = later_concentration_payload(payload, "20")
    expanded["access_scope"]["account_ids"] = full_scope
    command = expanded["concentration"]
    command["authorization"]["authorization_id"] = expanded_id
    account = deepcopy(command["position_snapshot"]["accounts"][1])
    account["account_id"] = third["account_id"]
    command["position_snapshot"]["accounts"].append(account)
    cost = deepcopy(command["liquidation_costs"][1])
    cost["account_id"] = third["account_id"]
    command["liquidation_costs"].append(cost)
    admitted = run_frozen_decision_case(
        migrated_settings, expanded, clock=GovernanceClock("2042-05-20T17:00:00+00:00")
    )
    assert admitted.report is not None and admitted.report.result.concentration is not None
    origin = admitted.report.result.concentration.issuers[0]
    original_basis = next(
        item for item in origin.obligation_quantity_basis if item.account_id == third["account_id"]
    )
    assert original_basis.quantity == 100
    narrowed_id = authorize_changed_scope(
        migrated_settings, expanded_id, proposal, full_scope[:2], "21", "22"
    )
    narrowed = later_concentration_payload(payload, "23")
    narrowed["concentration"]["authorization"]["authorization_id"] = narrowed_id
    denied = run_frozen_decision_case(
        migrated_settings, narrowed, clock=GovernanceClock("2042-05-23T17:00:00+00:00")
    )
    assert denied.report is not None and denied.report.result.concentration is not None
    assert denied.report.result.concentration.disposition == "BLOCKED"
    assert third["account_id"] not in {
        item.account_id
        for item in denied.report.result.concentration.issuers[0].obligation_quantity_basis
    }
    restored_id = authorize_changed_scope(
        migrated_settings, narrowed_id, proposal, full_scope, "24", "25"
    )
    restored = later_concentration_payload(expanded, "26")
    restored["concentration"]["authorization"]["authorization_id"] = restored_id
    record_synthetic_sale(restored, "100", "third-transfer", account_index=2)
    third_account = restored["concentration"]["position_snapshot"]["accounts"][2]
    third_account["ledger_entries"][-1].update(entry_type="TRANSFER_OUT", cash_delta="0")
    third_account["account_equity"] = "0"
    for field in ("ledger_cash", "trading_cash", "transferable_cash"):
        third_account["cash_state"][field] = "0"
    resumed = run_frozen_decision_case(
        migrated_settings, restored, clock=GovernanceClock("2042-05-26T17:00:00+00:00")
    )
    assert resumed.report is not None and resumed.report.result.concentration is not None
    basis = resumed.report.result.concentration.issuers[0].obligation_quantity_basis
    assert next(item for item in basis if item.account_id == third["account_id"]) == original_basis
    later = later_concentration_payload(restored, "27")
    record_synthetic_sale(later, "70", "partial-after-scope-denial")
    final = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-27T17:00:00+00:00")
    )
    assert final.report is not None and final.report.result.concentration is not None
    assert final.report.result.concentration.issuers[0].direction == "REDUCE"
