from copy import deepcopy
from decimal import Decimal

from test_liquidity_protection import liquidity_payload, set_liquidity_cash
from test_portfolio_authorization import (
    next_portfolio_proposal,
    portfolio_case_payload,
    portfolio_confirmation_command,
    portfolio_proposal,
)
from test_position_state_reconciliation import (
    position_snapshot_command,
    refresh_current_position_evidence,
)
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings


def test_unknown_sale_terms_preserve_the_known_cash_restoration_deficit(
    migrated_settings: Settings,
) -> None:
    payload = liquidity_payload(migrated_settings, "unknown-disposal")
    payload["liquidity"]["sale_terms"] = []
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.liquidity
    assert outcome is not None
    assert outcome.disposition == "EVIDENCE_FAILED"
    assert outcome.maximum_fundable_cash is None
    assert outcome.remediation_shortfall == Decimal("415.90")
    assert outcome.remediation_id == execution.decision_event_id


def test_confirmed_empty_account_has_a_known_funding_gap(
    migrated_settings: Settings,
) -> None:
    payload = liquidity_payload(
        migrated_settings, "empty-account", obligations=portfolio_proposal()["cash_obligations"]
    )
    snapshot = payload["liquidity"]["position_snapshot"]
    account = snapshot["accounts"][0]
    account.update(account_equity="0", positions=[], open_orders=[], ledger_entries=[])
    for field in (
        "opening_ledger_cash",
        "ledger_cash",
        "trading_cash",
        "transferable_cash",
        "frozen_cash",
        "receivable_cash",
        "payable_cash",
    ):
        account["cash_state"][field] = "0"
    snapshot["annotations"] = []
    payload["liquidity"]["sale_terms"] = []
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.liquidity
    assert outcome is not None
    assert outcome.disposition == "FUNDING_INFEASIBLE"
    assert outcome.net_liquidation_equity == Decimal("0")
    assert outcome.maximum_fundable_cash == Decimal("0")
    assert outcome.uncovered_obligation_gap == Decimal("125.50")


def test_valid_payment_survives_an_unrelated_cost_evidence_failure(
    migrated_settings: Settings,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    payload = liquidity_payload(migrated_settings, "payment-with-failure", obligations=obligations)
    payload["liquidity"]["settled_coverage"] = [
        {
            "receipt_id": "synthetic-independent-payment",
            "kind": "EXTERNAL_SETTLED_PAYMENT",
            "obligation_id": obligations[0]["obligation_id"],
            "amount": "25.50",
            "evidence": payload["liquidity"]["position_snapshot"]["snapshot_evidence"],
        }
    ]
    payload["liquidity"]["cost_evidence"] = None
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert first.report is not None
    assert first.report.result.liquidity is not None
    assert first.report.result.liquidity.disposition == "EVIDENCE_FAILED"
    later = deepcopy(payload)
    later["business_identity"] += ":recovered"
    later["case_id"] += "-recovered"
    later["liquidity"]["cost_evidence"] = later["liquidity"]["position_snapshot"][
        "snapshot_evidence"
    ]
    later["liquidity"]["settled_coverage"] = []
    recovered = run_frozen_decision_case(migrated_settings, later, clock=GovernanceClock())
    assert recovered.report is not None
    outcome = recovered.report.result.liquidity
    assert outcome is not None
    assert outcome.six_month_obligations == Decimal("100")
    assert len(outcome.settled_coverage) == 1


def test_valuation_only_target_change_does_not_confirm_restoration(
    migrated_settings: Settings,
) -> None:
    payload = liquidity_payload(migrated_settings, "valuation-restoration")
    set_liquidity_cash(payload, "200")
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    later = deepcopy(payload)
    later["business_identity"] += ":repriced"
    later["case_id"] += "-repriced"
    later["knowledge_cutoff"] = "2042-05-18T16:00:00Z"
    snapshot = later["liquidity"]["position_snapshot"]
    snapshot["cutoff_at"] = later["knowledge_cutoff"]
    refresh_current_position_evidence(snapshot, later["knowledge_cutoff"])
    snapshot["accounts"][0]["account_equity"] = "500"
    snapshot["accounts"][0]["positions"][0]["market_price"] = "3"
    repriced = run_frozen_decision_case(migrated_settings, later, clock=GovernanceClock())
    assert repriced.report is not None
    outcome = repriced.report.result.liquidity
    assert outcome is not None
    assert outcome.normal_cash_target == Decimal("195")
    assert outcome.remediation_shortfall == Decimal("0")
    assert outcome.remediation_id == first.decision_event_id
    assert "RESTORATION_CONFIRMATION_REQUIRED" in outcome.reasons
    assert outcome.disposition == "REMEDIATION_REQUIRED"
    assert outcome.new_exposure_blocked is True
    assert outcome.deployable_purchase_cash == Decimal("0")


def test_authorized_scope_expansion_preserves_restoration_and_settled_payments(
    migrated_settings: Settings,
) -> None:
    obligations = portfolio_proposal()["cash_obligations"]
    payload = liquidity_payload(migrated_settings, "before-expansion", obligations=obligations)
    set_liquidity_cash(payload, "200")
    payload["liquidity"]["settled_coverage"] = [
        {
            "receipt_id": "synthetic-before-expansion-payment",
            "kind": "EXTERNAL_SETTLED_PAYMENT",
            "obligation_id": obligations[0]["obligation_id"],
            "amount": "25.50",
            "evidence": payload["liquidity"]["position_snapshot"]["snapshot_evidence"],
        }
    ]
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    proposal = next_portfolio_proposal()
    for field in ("snapshot", "activation_snapshot"):
        proposal[field]["accounts"] = [
            account
            for account in proposal[field]["accounts"]
            if account["account_id"] in proposal[field]["selected_account_ids"]
        ]
    authorization = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "liquidity-expanded-authorization",
            portfolio_confirmation_command(
                proposal,
                previous_authorization_id=payload["liquidity"]["authorization_id"],
            ),
        ),
        clock=GovernanceClock("2042-05-19T16:01:00Z"),
    )
    assert authorization.report is not None
    assert authorization.report.result.portfolio is not None
    assert authorization.report.result.portfolio.disposition == "APPROVED"
    expanded = deepcopy(payload)
    expanded["business_identity"] += ":expanded"
    expanded["case_id"] += "-expanded"
    expanded["knowledge_cutoff"] = "2042-05-19T16:00:00Z"
    expanded["access_scope"]["account_ids"].append("synthetic-account-8029")
    command = expanded["liquidity"]
    command["authorization_id"] = authorization.decision_event_id
    command["settled_coverage"] = []
    snapshot = command["position_snapshot"]
    second = position_snapshot_command()["accounts"][1]
    second["account_equity"] = "1050"
    second["cash_state"].update(
        opening_ledger_cash="900",
        ledger_cash="450",
        trading_cash="450",
        transferable_cash="450",
    )
    snapshot["accounts"].append(second)
    snapshot["cutoff_at"] = expanded["knowledge_cutoff"]
    refresh_current_position_evidence(snapshot, expanded["knowledge_cutoff"])
    command["sale_terms"].append(
        {
            **deepcopy(command["sale_terms"][0]),
            "account_id": second["account_id"],
        }
    )
    for term in command["sale_terms"]:
        term["transferable_at"] = expanded["knowledge_cutoff"]
        term["evidence"] = snapshot["snapshot_evidence"]
    execution = run_frozen_decision_case(migrated_settings, expanded, clock=GovernanceClock())
    assert execution.report is not None
    outcome = execution.report.result.liquidity
    assert outcome is not None
    assert outcome.six_month_obligations == Decimal("100")
    assert outcome.qualified_cash == Decimal("650")
    assert outcome.disposition == "REMEDIATION_REQUIRED"
    assert outcome.remediation_shortfall == Decimal("210.50")
    assert outcome.remediation_id == first.decision_event_id
    narrowed = deepcopy(expanded)
    narrowed["access_scope"]["account_ids"].pop()
    narrowed["liquidity"]["position_snapshot"]["accounts"].pop()
    narrowed["liquidity"]["sale_terms"].pop()
    for attempt in range(2):
        narrowed["business_identity"] = f"{expanded['business_identity']}:narrowed-{attempt}"
        narrowed["case_id"] = f"{expanded['case_id']}-narrowed-{attempt}"
        denied = run_frozen_decision_case(migrated_settings, narrowed, clock=GovernanceClock())
        assert denied.report is not None
        denied_outcome = denied.report.result.liquidity
        assert denied_outcome is not None
        assert denied_outcome.reasons == ("LIQUIDITY_HISTORY_SCOPE_INCOMPLETE",)
        assert denied_outcome.new_exposure_blocked is True
        assert denied_outcome.remediation_id is None
        assert denied_outcome.retained_remediation_shortfall is None
