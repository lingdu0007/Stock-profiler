from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
from test_portfolio_authorization import (
    portfolio_case_payload,
    portfolio_confirmation_command,
    portfolio_fixture,
    portfolio_proposal,
    result_family,
)
from test_position_state_reconciliation import (
    position_case_payload,
    position_snapshot_command,
    refresh_current_position_evidence,
)
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.delivery.access import AccessPrincipal


def liquidity_payload(
    settings: Settings,
    identity: str,
    *,
    obligations: list[dict[str, Any]] | None = None,
    two_accounts: bool = False,
    portfolio_id: str | None = None,
    single_account_id: str | None = None,
) -> dict[str, Any]:
    proposal = portfolio_proposal()
    if portfolio_id is not None:
        proposal["portfolio_id"] = portfolio_id
    proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    if single_account_id is not None:
        assert not two_accounts
        proposal["snapshot"]["accounts"][0]["account_id"] = single_account_id
        proposal["snapshot"]["selected_account_ids"] = [single_account_id]
    if two_accounts:
        second = deepcopy(portfolio_fixture()["next_cash_account"])
        second["captured_at"] = proposal["snapshot"]["cutoff_at"]
        proposal["snapshot"]["accounts"].append(second)
        proposal["snapshot"]["selected_account_ids"].append(second["account_id"])
    proposal["cash_obligations"] = obligations or []
    authorization = run_frozen_decision_case(
        settings,
        portfolio_case_payload(
            settings, "liquidity-authorization", portfolio_confirmation_command(proposal)
        ),
        clock=GovernanceClock(),
    )
    assert authorization.report is not None, authorization.model_dump_json()
    assert authorization.report.result.portfolio is not None
    assert authorization.report.result.portfolio.disposition == "APPROVED", (
        authorization.report.result.portfolio.reasons
    )
    snapshot = position_snapshot_command()
    if not two_accounts:
        snapshot["accounts"] = snapshot["accounts"][:1]
    if single_account_id is not None:
        snapshot["accounts"][0]["account_id"] = single_account_id
        for annotation in snapshot.get("annotations", []):
            annotation["account_id"] = single_account_id
    payload = position_case_payload(settings, identity, snapshot)
    payload.pop("position")
    payload["version_bundle"].update(
        case_contract_version="8.2.0",
        host_contract_version="8.2.0",
        report_projection_contract_version="8.2.0",
    )
    payload["liquidity"] = {
        "operation": "LIQUIDITY_ASSESS",
        "contract_version": "1.0.0",
        "synthetic": True,
        "generator_version": "liquidity-fixture-v1",
        "seed": 6381,
        "portfolio_id": proposal["portfolio_id"],
        "authorization_id": authorization.decision_event_id,
        "position_snapshot": snapshot,
        "expected_purchase_fees": "0",
        "expected_liquidation_fees": "0",
        "sale_terms": [
            {
                "account_id": account["account_id"],
                "security_id": "XQZ-4017",
                "minimum_quantity": "10",
                "quantity_increment": "10",
                "allow_full_odd_lot": True,
                "commission_ratio": "0",
                "minimum_commission": "0",
                "other_cost_ratio": "0",
                "transferable_at": "2042-05-18T16:00:00Z",
                "evidence": snapshot["snapshot_evidence"],
            }
            for account in snapshot["accounts"]
        ],
        "transfer_routes": [],
        "settled_coverage": [],
        "cost_evidence": snapshot["snapshot_evidence"],
    }
    return payload


def test_liquidity_reserve_is_committed_readable_and_replayable(
    migrated_settings: Settings,
) -> None:
    payload = liquidity_payload(migrated_settings, "cash-reserve")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())

    assert first.report is not None
    outcome = first.report.result.model_dump()["liquidity"]
    assert outcome["disposition"] == "REMEDIATION_REQUIRED"
    assert outcome["net_liquidation_equity"] == Decimal("1310")
    assert outcome["normal_cash_target"] == Decimal("510.90")
    assert outcome["hard_cash_floor"] == Decimal("301.30")
    assert outcome["qualified_cash"] == Decimal("95")
    assert outcome["deployable_purchase_cash"] == Decimal("0")
    assert outcome["remediation_shortfall"] == Decimal("415.90")
    assert outcome["new_exposure_blocked"] is True
    assert outcome["authorization_id"] == payload["liquidity"]["authorization_id"]
    assert outcome["position_snapshot"]["snapshot"]["cash_states"][0]["ledger_cash"] == Decimal(
        "100"
    )
    assert (
        get_formal_report(
            first.report_version_id,
            migrated_settings,
            principal=AccessPrincipal(
                user_id="stock-profiler-single-user",
                account_ids=("synthetic-account-4017",),
                permissions=("REPORT_READ",),
            ),
        )
        == first.report
    )
    replay = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert replay.report == first.report
    assert replay.framework_run_id == first.framework_run_id


def test_dated_obligation_adjusts_reserves_without_double_counting_equity(
    migrated_settings: Settings,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    payload = liquidity_payload(migrated_settings, "dated-obligation", obligations=obligations)
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None
    outcome = first.report.result.model_dump()["liquidity"]
    assert outcome["net_liquidation_equity"] == Decimal("1310")
    assert outcome["six_month_obligations"] == Decimal("125.50")
    assert outcome["normal_cash_target"] == Decimal("587.455")
    assert outcome["hard_cash_floor"] == Decimal("397.935")
    assert outcome["remediation_shortfall"] == Decimal("492.455")
    assert outcome["deployable_purchase_cash"] == Decimal("0")


def set_liquidity_cash(payload: dict[str, Any], cash: str) -> None:
    account = payload["liquidity"]["position_snapshot"]["accounts"][0]
    value = Decimal(cash)
    stock_value = Decimal("1000") - value
    account["account_equity"] = "1000"
    account["cash_state"].update(
        opening_ledger_cash="1000",
        ledger_cash=cash,
        trading_cash=cash,
        transferable_cash=cash,
        frozen_cash="0",
        receivable_cash="0",
        payable_cash="0",
    )
    account["positions"][0].update(
        broker_sellable_quantity="100",
        unsettled_quantity="0",
        frozen_quantity="0",
        open_sell_order_quantity="0",
        reported_cost_basis=str(stock_value),
        market_price=str(stock_value / 100),
    )
    account["open_orders"] = []
    account["ledger_entries"] = account["ledger_entries"][:1]
    account["ledger_entries"][0].update(
        cost_basis_delta=str(stock_value), cash_delta=str(-stock_value)
    )


@pytest.mark.parametrize(
    ("cash", "disposition", "blocked", "shortfall", "deployable"),
    [
        ("400", "AVAILABLE", False, "0", "10"),
        ("390", "ZERO_DEPLOYABLE_CASH", False, "0", "0"),
        ("389", "ZERO_DEPLOYABLE_CASH", True, "0", "0"),
        ("230", "ZERO_DEPLOYABLE_CASH", True, "0", "0"),
        ("229", "REMEDIATION_REQUIRED", True, "161", "0"),
    ],
)
def test_liquidity_cash_threshold_boundaries(
    migrated_settings: Settings,
    cash: str,
    disposition: str,
    blocked: bool,
    shortfall: str,
    deployable: str,
) -> None:
    payload = liquidity_payload(migrated_settings, f"boundary-{cash}")
    set_liquidity_cash(payload, cash)
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["disposition"] == disposition
    assert outcome["new_exposure_blocked"] is blocked
    assert outcome["remediation_shortfall"] == Decimal(shortfall)
    assert outcome["deployable_purchase_cash"] == Decimal(deployable)


def test_final_broker_cash_deducts_buy_reserves_once_and_retains_purchase_fees(
    migrated_settings: Settings,
) -> None:
    payload = liquidity_payload(migrated_settings, "buy-reserves")
    set_liquidity_cash(payload, "450")
    command = payload["liquidity"]
    account = command["position_snapshot"]["accounts"][0]
    account["cash_state"].update(trading_cash="430", transferable_cash="430", frozen_cash="20")
    account["open_orders"] = [
        {
            "order_id": "synthetic-reserved-buy",
            "security_id": "XQZ-4017",
            "side": "BUY",
            "remaining_quantity": "1",
            "reserved_cash": "20",
            "reserved_cash_semantics": "BROKER_FINAL_RESERVED_CASH",
            "evidence": account["cash_state"]["evidence"],
        }
    ]
    command["expected_purchase_fees"] = "10"
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["qualified_cash"] == Decimal("420")
    assert outcome["deployable_purchase_cash"] == Decimal("30")
    assert outcome["reserved_buy_cash"] == Decimal("20")


def test_expired_authorization_does_not_turn_available_cash_into_buying_permission(
    migrated_settings: Settings,
) -> None:
    payload = liquidity_payload(migrated_settings, "expired")
    set_liquidity_cash(payload, "450")
    cutoff = "2042-11-18T16:00:00Z"
    payload["knowledge_cutoff"] = cutoff
    snapshot = payload["liquidity"]["position_snapshot"]
    snapshot["cutoff_at"] = cutoff
    refresh_current_position_evidence(snapshot, cutoff)
    payload["liquidity"]["sale_terms"][0]["transferable_at"] = cutoff
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["new_exposure_blocked"] is True
    assert outcome["deployable_purchase_cash"] == Decimal("0")
    assert "RISK_BUDGET_EXPIRED" in outcome["reasons"]
    assert outcome["normal_cash_target"] == Decimal("390")


def test_obligation_not_below_equity_shows_maximum_legal_funding_and_uncovered_gap(
    migrated_settings: Settings,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    obligations[0]["amount"] = "1310"
    payload = liquidity_payload(migrated_settings, "funding-infeasible", obligations=obligations)
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["disposition"] == "FUNDING_INFEASIBLE"
    assert outcome["maximum_fundable_cash"] == Decimal("930")
    assert outcome["uncovered_obligation_gap"] == Decimal("380")
    assert outcome["deployable_purchase_cash"] == Decimal("0")
    assert outcome["new_exposure_blocked"] is True
    assert outcome["maximum_funding_plan"][0]["quantity"] == Decimal("70")
    assert outcome["maximum_funding_plan"][0]["net_proceeds"] == Decimal("840")
    assert outcome["qualified_cash"] == Decimal("95")


def test_partial_cash_restoration_keeps_the_original_normal_target_obligation(
    migrated_settings: Settings,
) -> None:
    payload = liquidity_payload(migrated_settings, "restoration-original")
    set_liquidity_cash(payload, "200")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None
    assert first.report.result.model_dump()["liquidity"]["remediation_shortfall"] == Decimal("190")
    later = deepcopy(payload)
    later["business_identity"] += ":partial"
    later["case_id"] += "-partial"
    later["knowledge_cutoff"] = "2042-05-18T16:00:00Z"
    snapshot = later["liquidity"]["position_snapshot"]
    snapshot["snapshot_id"] += "-partial"
    snapshot["cutoff_at"] = later["knowledge_cutoff"]
    refresh_current_position_evidence(snapshot, later["knowledge_cutoff"])
    account = snapshot["accounts"][0]
    account["cash_state"].update(ledger_cash="280", trading_cash="280", transferable_cash="280")
    account["positions"][0].update(
        total_quantity="90", broker_sellable_quantity="90", reported_cost_basis="720"
    )
    sale = deepcopy(account["ledger_entries"][0])
    sale.update(
        entry_id="synthetic-cash-restoration-fill",
        quantity_delta="-10",
        cost_basis_delta="-80",
        cash_delta="80",
        occurred_at="2042-05-18T15:00:00Z",
        evidence=deepcopy(snapshot["snapshot_evidence"]),
    )
    account["ledger_entries"].append(sale)
    later["liquidity"]["sale_terms"][0]["evidence"] = snapshot["snapshot_evidence"]
    partial = run_frozen_decision_case(migrated_settings, later, clock=GovernanceClock())
    assert partial.report is not None
    outcome = partial.report.result.model_dump()["liquidity"]
    assert outcome["disposition"] == "REMEDIATION_REQUIRED"
    assert outcome["remediation_shortfall"] == Decimal("110")
    assert outcome["remediation_id"] == first.decision_event_id
    assert outcome["new_exposure_blocked"] is True
    repriced = deepcopy(later)
    repriced["business_identity"] += ":repriced"
    repriced["case_id"] += "-repriced"
    repriced["knowledge_cutoff"] = "2042-05-19T16:00:00Z"
    repriced_snapshot = repriced["liquidity"]["position_snapshot"]
    repriced_snapshot["cutoff_at"] = repriced["knowledge_cutoff"]
    refresh_current_position_evidence(repriced_snapshot, repriced["knowledge_cutoff"])
    repriced_snapshot["accounts"][0]["account_equity"] = "550"
    repriced_snapshot["accounts"][0]["positions"][0]["market_price"] = "3"
    repriced["liquidity"]["sale_terms"][0]["transferable_at"] = repriced["knowledge_cutoff"]
    repriced_execution = run_frozen_decision_case(
        migrated_settings, repriced, clock=GovernanceClock()
    )
    assert repriced_execution.report is not None
    repriced_outcome = repriced_execution.report.result.liquidity
    assert repriced_outcome is not None
    assert repriced_outcome.normal_cash_target == Decimal("214.50")
    assert repriced_outcome.remediation_id == first.decision_event_id
    assert repriced_outcome.disposition == "REMEDIATION_REQUIRED"
    assert repriced_outcome.new_exposure_blocked is True


def test_evidence_failure_preserves_an_established_cash_restoration_obligation(
    migrated_settings: Settings,
) -> None:
    payload = liquidity_payload(migrated_settings, "before-evidence-failure")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None
    later = deepcopy(payload)
    later["business_identity"] += ":missing"
    later["case_id"] += "-missing"
    later["liquidity"]["position_snapshot"]["accounts"][0]["cash_state"]["trading_cash"] = None
    failed = run_frozen_decision_case(migrated_settings, later, clock=GovernanceClock())
    assert failed.report is not None
    outcome = failed.report.result.model_dump()["liquidity"]
    assert outcome["disposition"] == "EVIDENCE_FAILED"
    assert outcome["qualified_cash"] is None
    assert outcome["deployable_purchase_cash"] is None
    assert outcome["remediation_id"] == first.decision_event_id
    assert outcome["retained_remediation_shortfall"] == Decimal("415.90")
    assert outcome["new_exposure_blocked"] is True


def test_late_sale_proceeds_cannot_cover_an_earlier_cash_deadline(
    migrated_settings: Settings,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    obligations[0].update(amount="200", latest_usable_at="2042-05-17T16:00:00Z")
    payload = liquidity_payload(migrated_settings, "late-sale", obligations=obligations)
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["disposition"] == "FUNDING_INFEASIBLE"
    assert outcome["maximum_fundable_cash"] == Decimal("90")
    assert outcome["uncovered_obligation_gap"] == Decimal("110")
    assert outcome["maximum_funding_plan"] == ()


@pytest.mark.parametrize(("capacity", "gap", "maximum"), [("70", "0", "1000"), ("60", "10", "990")])
def test_cross_account_funding_respects_verified_route_capacity(
    migrated_settings: Settings, capacity: str, gap: str, maximum: str
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    obligations[0]["amount"] = "1000"
    payload = liquidity_payload(
        migrated_settings, "routed-funding", obligations=obligations, two_accounts=True
    )
    payload["liquidity"]["transfer_routes"] = [
        {
            "route_id": "synthetic-cash-route",
            "source_account_id": "synthetic-account-8029",
            "target_account_id": "synthetic-account-4017",
            "departure_at": "2042-05-19T16:00:00Z",
            "arrival_at": "2042-05-20T16:00:00Z",
            "capacity": capacity,
            "evidence": payload["liquidity"]["position_snapshot"]["snapshot_evidence"],
        }
    ]
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["maximum_fundable_cash"] == Decimal(maximum)
    assert outcome["uncovered_obligation_gap"] == Decimal(gap)
    assert outcome["obligation_funding"][0]["maximum_covered_cash"] == Decimal(maximum)


def test_settled_external_payment_reduces_obligation_but_never_increases_equity(
    migrated_settings: Settings,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    payload = liquidity_payload(migrated_settings, "settled-coverage", obligations=obligations)
    payload["liquidity"]["settled_coverage"] = [
        {
            "receipt_id": "synthetic-external-settlement",
            "kind": "EXTERNAL_SETTLED_PAYMENT",
            "obligation_id": obligations[0]["obligation_id"],
            "amount": "25.50",
            "evidence": payload["liquidity"]["position_snapshot"]["snapshot_evidence"],
        }
    ]
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["net_liquidation_equity"] == Decimal("1310")
    assert outcome["six_month_obligations"] == Decimal("100")
    assert outcome["normal_cash_target"] == Decimal("571.90")
    assert outcome["obligation_funding"][0]["required_cash"] == Decimal("100")


def test_missing_cost_evidence_is_not_a_zero_cost_funding_plan(
    migrated_settings: Settings,
) -> None:
    payload = liquidity_payload(migrated_settings, "missing-cost")
    payload["liquidity"]["cost_evidence"] = None
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["disposition"] == "EVIDENCE_FAILED"
    assert outcome["net_liquidation_equity"] is None
    assert "LIQUIDITY_COST_EVIDENCE_FAILED" in outcome["reasons"]


@pytest.mark.parametrize("cash", ["200", "450"])
def test_business_failure_keeps_protection_without_authorizing_new_exposure(
    migrated_settings: Settings, cash: str
) -> None:
    payload = liquidity_payload(migrated_settings, "failed-business")
    set_liquidity_cash(payload, cash)
    source = result_family("result-failed")
    payload["input"] = source["input"]
    payload["expected_external_result"] = source["expected_external_result"]
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["normal_cash_target"] == Decimal("390")
    assert outcome["new_exposure_blocked"] is True
    assert outcome["deployable_purchase_cash"] == Decimal("0")
    assert "BUSINESS_PREREQUISITE_NOT_MET" in outcome["reasons"]
    assert outcome["remediation_shortfall"] == (Decimal("190") if cash == "200" else Decimal("0"))


@pytest.mark.parametrize("include_terms", [True, False])
def test_confirmed_sell_restriction_is_zero_legal_funding_not_unknown_evidence(
    migrated_settings: Settings,
    include_terms: bool,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    payload = liquidity_payload(migrated_settings, "restricted-sale", obligations=obligations)
    snapshot = payload["liquidity"]["position_snapshot"]
    snapshot["accounts"][0]["execution_restrictions"] = [
        {
            "restriction_id": "synthetic-sell-suspension",
            "security_id": "XQZ-4017",
            "kind": "SELL_BLOCK",
            "reason": "Synthetic confirmed sell suspension.",
            "active": True,
            "evidence": snapshot["snapshot_evidence"],
        }
    ]
    if not include_terms:
        payload["liquidity"]["sale_terms"] = []
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["disposition"] == "FUNDING_INFEASIBLE"
    assert outcome["maximum_fundable_cash"] == Decimal("90")
    assert outcome["uncovered_obligation_gap"] == Decimal("35.50")
    assert outcome["maximum_funding_plan"] == ()


def test_routed_sale_plan_distinguishes_net_proceeds_from_deadline_funding(
    migrated_settings: Settings,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    obligations[0]["amount"] = "1000"
    payload = liquidity_payload(
        migrated_settings, "routed-sale-allocation", obligations=obligations, two_accounts=True
    )
    snapshot = payload["liquidity"]["position_snapshot"]
    snapshot["accounts"][1]["cash_state"]["transferable_cash"] = "0"
    payload["liquidity"]["transfer_routes"] = [
        {
            "route_id": "synthetic-capacity-limited-route",
            "source_account_id": "synthetic-account-8029",
            "target_account_id": "synthetic-account-4017",
            "departure_at": "2042-05-19T16:00:00Z",
            "arrival_at": "2042-05-20T16:00:00Z",
            "capacity": "60",
            "evidence": snapshot["snapshot_evidence"],
        }
    ]
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["maximum_fundable_cash"] == Decimal("990")
    routed_leg = next(
        item
        for item in outcome["maximum_funding_plan"]
        if item["account_id"] == "synthetic-account-8029"
    )
    assert routed_leg["net_proceeds"] > Decimal("60")
    assert routed_leg["deadline_funding_contribution"] == Decimal("60")


@pytest.mark.parametrize(
    ("deadline", "included"),
    [
        ("2042-11-17T16:00:00Z", True),
        ("2042-11-17T16:00:01Z", False),
    ],
)
def test_six_calendar_month_boundary_keeps_farther_obligations_reserved(
    migrated_settings: Settings,
    deadline: str,
    included: bool,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    obligations[0].update(amount="100", latest_usable_at=deadline)
    payload = liquidity_payload(migrated_settings, "calendar-boundary", obligations=obligations)
    set_liquidity_cash(payload, "600")
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["six_month_obligations"] == Decimal("100" if included else "0")
    assert outcome["normal_cash_target"] == Decimal("451" if included else "390")
    assert outcome["deployable_purchase_cash"] == Decimal("149" if included else "110")


@pytest.mark.parametrize(
    "source", ["future_income", "unused_credit", "pending_deposit", "outside_assets"]
)
def test_unconfirmed_external_funding_is_not_an_accepted_cash_input(
    migrated_settings: Settings,
    source: str,
) -> None:
    payload = liquidity_payload(migrated_settings, "unconfirmed-funding")
    payload["liquidity"][source] = "9999"
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())


def test_sale_costs_and_legal_lots_reduce_hypothetical_funding(
    migrated_settings: Settings,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    obligations[0]["amount"] = "1310"
    payload = liquidity_payload(migrated_settings, "costed-funding", obligations=obligations)
    payload["liquidity"]["sale_terms"][0].update(
        minimum_quantity="20",
        quantity_increment="20",
        commission_ratio="0.001",
        minimum_commission="3",
        other_cost_ratio="0.01",
    )
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    leg = outcome["maximum_funding_plan"][0]
    assert leg["quantity"] == Decimal("60")
    assert leg["gross_proceeds"] == Decimal("720")
    assert leg["disposal_cost"] == Decimal("10.20")
    assert outcome["maximum_fundable_cash"] == Decimal("799.80")
    assert outcome["uncovered_obligation_gap"] == Decimal("510.20")


def test_multiple_obligations_cannot_reuse_the_same_transferable_cash(
    migrated_settings: Settings,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    obligations[0].update(amount="60", latest_usable_at="2042-05-17T16:00:00Z")
    second = deepcopy(obligations[0])
    second["obligation_id"] += "-second"
    obligations.append(second)
    payload = liquidity_payload(migrated_settings, "no-double-funding", obligations=obligations)
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.model_dump()["liquidity"]
    assert outcome["maximum_fundable_cash"] == Decimal("90")
    assert outcome["uncovered_obligation_gap"] == Decimal("30")
    assert sum(item["maximum_covered_cash"] for item in outcome["obligation_funding"]) == Decimal(
        "90"
    )


@pytest.mark.parametrize(
    ("funding_evidence_complete", "repair_fee"),
    [(True, "0"), (False, "0"), (False, "5"), (False, "20")],
)
def test_restoration_release_requires_confirmed_cash_and_complete_funding_evidence(
    migrated_settings: Settings,
    funding_evidence_complete: bool,
    repair_fee: str,
) -> None:
    payload = liquidity_payload(migrated_settings, "restoration-release")
    set_liquidity_cash(payload, "200")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    later = deepcopy(payload)
    later["business_identity"] += ":restored"
    later["case_id"] += "-restored"
    later["knowledge_cutoff"] = "2042-05-18T16:00:00Z"
    snapshot = later["liquidity"]["position_snapshot"]
    snapshot["snapshot_id"] += "-restored"
    snapshot["cutoff_at"] = later["knowledge_cutoff"]
    refresh_current_position_evidence(snapshot, later["knowledge_cutoff"])
    account = snapshot["accounts"][0]
    account["cash_state"].update(ledger_cash="400", trading_cash="400", transferable_cash="400")
    account["positions"][0].update(
        total_quantity="75", broker_sellable_quantity="75", reported_cost_basis="600"
    )
    sale = deepcopy(account["ledger_entries"][0])
    sale.update(
        entry_id="synthetic-restoration-completed-fill",
        quantity_delta="-25",
        cost_basis_delta="-200",
        cash_delta="200",
        occurred_at="2042-05-18T15:00:00Z",
        evidence=deepcopy(snapshot["snapshot_evidence"]),
    )
    account["ledger_entries"].append(sale)
    if not funding_evidence_complete:
        later["liquidity"]["sale_terms"][0]["evidence"] = {
            **snapshot["snapshot_evidence"],
            "validated_at": None,
        }
    restored = run_frozen_decision_case(migrated_settings, later, clock=GovernanceClock())
    assert restored.report is not None
    outcome = restored.report.result.model_dump()["liquidity"]
    if funding_evidence_complete:
        assert outcome["remediation_id"] is None
        assert outcome["remediation_shortfall"] == Decimal("0")
        assert outcome["disposition"] == "AVAILABLE"
        assert outcome["deployable_purchase_cash"] == Decimal("10")
    else:
        assert outcome["disposition"] == "EVIDENCE_FAILED"
        assert outcome["remediation_id"] == first.decision_event_id
        assert outcome["retained_remediation_shortfall"] == Decimal("190")
        assert outcome["new_exposure_blocked"] is True
        assert outcome["restoration_cash_confirmed"] is True
        recovered = deepcopy(later)
        recovered["business_identity"] += ":funding-evidence-restored"
        recovered["case_id"] += "-funding-evidence-restored"
        recovered["liquidity"]["sale_terms"][0]["evidence"] = snapshot["snapshot_evidence"]
        if Decimal(repair_fee) > 0:
            recovered["knowledge_cutoff"] = "2042-05-19T16:00:00Z"
            recovered_snapshot = recovered["liquidity"]["position_snapshot"]
            recovered_snapshot["cutoff_at"] = recovered["knowledge_cutoff"]
            refresh_current_position_evidence(recovered_snapshot, recovered["knowledge_cutoff"])
            recovered_account = recovered_snapshot["accounts"][0]
            cash_after_fee = str(Decimal("400") - Decimal(repair_fee))
            recovered_account["account_equity"] = str(Decimal("1000") - Decimal(repair_fee))
            recovered_account["cash_state"].update(
                ledger_cash=cash_after_fee,
                trading_cash=cash_after_fee,
                transferable_cash=cash_after_fee,
            )
            recovered_account["ledger_entries"].append(
                {
                    "entry_id": "synthetic-restoration-account-fee",
                    "entry_type": "FEE",
                    "security_id": None,
                    "quantity_delta": "0",
                    "cost_basis_delta": "0",
                    "cash_delta": str(-Decimal(repair_fee)),
                    "occurred_at": "2042-05-19T15:00:00Z",
                    "evidence": deepcopy(recovered_snapshot["snapshot_evidence"]),
                }
            )
            recovered["liquidity"]["sale_terms"][0].update(
                transferable_at=recovered["knowledge_cutoff"],
                evidence=recovered_snapshot["snapshot_evidence"],
            )
        execution = run_frozen_decision_case(migrated_settings, recovered, clock=GovernanceClock())
        assert execution.report is not None
        recovered_outcome = execution.report.result.liquidity
        assert recovered_outcome is not None
        if repair_fee == "20":
            assert recovered_outcome.disposition == "REMEDIATION_REQUIRED"
            assert recovered_outcome.remediation_id == first.decision_event_id
            assert recovered_outcome.remediation_shortfall == Decimal("2.20")
            assert recovered_outcome.restoration_cash_confirmed is False
        else:
            assert recovered_outcome.disposition == "AVAILABLE"
            assert recovered_outcome.remediation_id is None
        assert recovered_outcome.qualified_cash == Decimal("400") - Decimal(repair_fee)
