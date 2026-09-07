from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, Inexact, localcontext
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_portfolio_authorization import (
    portfolio_case_payload,
    portfolio_confirmation_command,
    portfolio_proposal,
    relaxation_evidence,
    relaxed_portfolio_proposal,
    scheduled_portfolio_proposal,
)
from test_position_state_reconciliation import (
    position_case_payload,
    position_evidence,
    position_snapshot_command,
    refresh_current_position_evidence,
)
from test_scoped_qualification import GovernanceClock

from stock_profiler.adapters.authentication.passkeys import PasskeyAuthenticator
from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.http.app import create_app
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase


def stress_authorization(
    settings: Settings,
    *,
    shock: str = "0.23",
    friction: str = "0.017",
    retain_excluded: bool = False,
    cash_obligations: bool = True,
) -> str:
    proposal = stress_proposal(shock=shock, friction=friction)
    if not retain_excluded:
        proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    if not cash_obligations:
        proposal["cash_obligations"] = []
    result = run_frozen_decision_case(
        settings,
        portfolio_case_payload(
            settings, "stress-authorization", portfolio_confirmation_command(proposal)
        ),
        clock=GovernanceClock(),
    )
    assert result.report is not None
    assert result.report.result.portfolio is not None
    authorization = result.report.result.portfolio.authorization
    assert authorization is not None
    return authorization.authorization_id


def stress_proposal(*, shock: str = "0.23", friction: str = "0.017") -> dict[str, Any]:
    proposal = portfolio_proposal()
    proposal["risk_budget"]["downside_grid"] = ["0.04", "0.09", shock]
    proposal["risk_budget"]["stress_calculation"] = {
        "contract_version": "1.0.0",
        "version_id": "synthetic-gross-stress-v1",
        "horizon_market_days": 20,
        "shock_ratio": shock,
        "disposal_friction_ratio": friction,
        "registered_at": "2042-05-16T16:00:00Z",
    }
    return proposal


def stress_successor(
    settings: Settings, authorization_id: str, *, friction: str = "0.017"
) -> dict[str, Any]:
    proposal = scheduled_portfolio_proposal()
    for name in ("snapshot", "activation_snapshot"):
        proposal[name]["accounts"] = proposal[name]["accounts"][:1]
    original = stress_proposal(friction=friction)["risk_budget"]
    for name in ("downside_grid", "stress_calculation"):
        proposal["risk_budget"][name] = original[name]
    if friction != "0.017":
        proposal["risk_budget"]["stress_calculation"]["version_id"] = "synthetic-stress-v2"
    return portfolio_case_payload(
        settings,
        "stress-successor",
        portfolio_confirmation_command(proposal, previous_authorization_id=authorization_id),
    )


def stress_payload(
    settings: Settings, authorization_id: str, identity: str = "normal"
) -> dict[str, Any]:
    snapshot = position_snapshot_command()
    snapshot["accounts"] = snapshot["accounts"][:1]
    account = snapshot["accounts"][0]
    # Independent synthetic example: 1200 stock value, 1820.4 cash/receivables,
    # 20.4 liquidation friction, 3000 net liquidation equity, 296.4 gross loss.
    account["account_equity"] = "3020.4"
    account["cash_state"]["opening_ledger_cash"] = "2612.4"
    account["cash_state"]["ledger_cash"] = "1810.4"
    payload = position_case_payload(settings, f"stress-{identity}", snapshot)
    payload.pop("position")
    payload["version_bundle"].update(
        case_contract_version="8.0.0",
        host_contract_version="8.0.0",
        report_projection_contract_version="8.0.0",
    )
    payload["stress"] = {
        "operation": "PORTFOLIO_STRESS_ASSESS",
        "contract_version": "1.0.0",
        "portfolio_id": "synthetic-decision-portfolio-alpha",
        "authorization_id": authorization_id,
        "position_snapshot": snapshot,
    }
    return payload


def test_normal_stress_is_committed_without_replacing_the_business_result(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_payload(migrated_settings, authorization_id)
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.publication_status == "PUBLISHED"
    assert execution.report is not None
    report = execution.report.model_dump(mode="json")
    assert report["result"]["outcome_code"] == "SYNTHETIC_REVIEW_COMPLETE"
    stress = report["result"]["stress"]
    assert stress["state"] == "NORMAL", report["result"]
    assert stress["risk_budget_version_id"] == "synthetic-risk-budget-alpha"
    assert stress["calculation_policy"]["shock_ratio"] == "0.23"
    assert stress["budget"]["hard_ratio"] == "0.18"
    assert stress["gross_stress_loss"] == "296.400"
    assert stress["net_liquidation_equity"] == "3000.000"
    assert stress["stress_ratio"] == "0.0988"
    assert stress["new_exposure_blocked"] is False
    assert stress["obligation"] is None
    assert len(stress["contributions"]) == 1
    assert stress["contributions"][0]["current_exposure"] == "1200"
    replay = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert replay.report == execution.report
    assert replay.framework_run_id == execution.framework_run_id
    token, _ = PasskeyAuthenticator(
        initialize_runtime_storage(migrated_settings).engine, migrated_settings
    )._create_session("synthetic-stress-report-session")
    client = TestClient(create_app(migrated_settings), base_url="https://localhost")
    client.cookies.set(migrated_settings.auth_session_cookie_name, token)
    response = client.get(f"/api/v1/reports/{execution.report.report_version_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["result"]["stress"] == stress


@pytest.mark.parametrize(
    ("equity", "cash", "opening", "state"),
    [
        ("2137.543", "927.543", "1729.543", "NORMAL"),
        ("2137.542", "927.542", "1729.542", "BUFFER"),
        ("1667.067", "457.067", "1259.067", "BUFFER"),
        ("1667.066", "457.066", "1259.066", "HARD_BREACH"),
    ],
)
def test_thresholds_preserve_stock_conclusions_and_create_only_a_portfolio_obligation(
    migrated_settings: Settings, equity: str, cash: str, opening: str, state: str
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_payload(migrated_settings, authorization_id)
    account = payload["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = equity
    account["cash_state"]["ledger_cash"] = cash
    account["cash_state"]["opening_ledger_cash"] = opening
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    result = execution.report.model_dump(mode="json")["result"]
    stress = result["stress"]
    assert stress["state"] == state
    assert result["outcome_code"] == "SYNTHETIC_REVIEW_COMPLETE"
    assert stress["new_exposure_blocked"] is (state != "NORMAL")
    if state == "HARD_BREACH":
        assert stress["obligation"]["direction"] == "REDUCE_TOTAL_STOCK_EXPOSURE"
        assert stress["obligation"]["target_stress_ratio"] == "0.14"
        assert stress["obligation"]["status"] == "OUTSTANDING"
        assert "security_id" not in stress["obligation"]
    else:
        assert stress["obligation"] is None


def next_stress_case(payload: dict[str, Any], day: str) -> dict[str, Any]:
    updated = deepcopy(payload)
    updated["business_identity"] += f":{day}"
    updated["case_id"] += f"-{day}"
    cutoff = f"2042-05-{day}T16:00:00Z"
    updated["knowledge_cutoff"] = cutoff
    snapshot = updated["stress"]["position_snapshot"]
    snapshot["snapshot_id"] += f"-{day}"
    snapshot["cutoff_at"] = cutoff
    refresh_current_position_evidence(snapshot, cutoff)
    return updated


def stress_result(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    execution = run_frozen_decision_case(
        settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert execution.report is not None
    return cast(dict[str, Any], execution.report.model_dump(mode="json")["result"]["stress"])


@pytest.mark.parametrize("partial_reversal", [False, True])
def test_restoration_survives_unknown_evidence_and_requires_a_reconciled_fill(
    migrated_settings: Settings,
    partial_reversal: bool,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    first = stress_payload(migrated_settings, authorization_id, "breach")
    account = first["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = "1667.066"
    account["cash_state"]["ledger_cash"] = "457.066"
    account["cash_state"]["opening_ledger_cash"] = "1259.066"
    breach = stress_result(migrated_settings, first)
    assert breach["state"] == "HARD_BREACH"
    pending = next_stress_case(first, "18")
    still_open = stress_result(migrated_settings, pending)
    assert still_open["obligation"]["obligation_id"] == breach["obligation"]["obligation_id"]
    assert still_open["gross_stress_loss"] == breach["gross_stress_loss"]
    unknown = next_stress_case(first, "19")
    unknown["stress"]["position_snapshot"]["accounts"][0]["positions"][0]["market_price"] = None
    blocked = stress_result(migrated_settings, unknown)
    assert blocked["state"] == "UNKNOWN"
    assert blocked["obligation"]["obligation_id"] == breach["obligation"]["obligation_id"]
    assert blocked["obligation"]["status"] == "OUTSTANDING"
    assert blocked["execution_blocked"] is True
    sold = next_stress_case(first, "20")
    account = sold["stress"]["position_snapshot"]["accounts"][0]
    account["positions"][0].update(
        total_quantity="50", broker_sellable_quantity="30", reported_cost_basis="450"
    )
    account["cash_state"]["ledger_cash"] = "1057.066"
    account["ledger_entries"].append(
        {
            "entry_id": "synthetic-restoration-sale",
            "entry_type": "FILL",
            "security_id": "XQZ-4017",
            "quantity_delta": "-50",
            "cost_basis_delta": "-450",
            "cash_delta": "600",
            "occurred_at": "2042-05-20T15:00:00Z",
            "evidence": position_evidence(
                "synthetic-restoration-sale", cutoff_at=sold["knowledge_cutoff"]
            ),
        }
    )
    restored = stress_result(migrated_settings, sold)
    assert restored["state"] == "NORMAL"
    assert restored["gross_stress_loss"] == "148.200"
    assert restored["obligation"]["obligation_id"] == breach["obligation"]["obligation_id"]
    assert restored["obligation"]["status"] == "SATISFIED"
    assert restored["new_exposure_blocked"] is False
    reversed_sale = next_stress_case(sold, "21")
    account = reversed_sale["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = "1667.066" if partial_reversal else "767.066"
    account["cash_state"]["ledger_cash"] = "577.066" if partial_reversal else "457.066"
    account["positions"][0].update(
        total_quantity="90" if partial_reversal else "100",
        reported_cost_basis="810" if partial_reversal else "900",
        market_price="12" if partial_reversal else "3",
    )
    account["ledger_entries"].append(
        {
            "entry_id": "synthetic-late-reversal",
            "entry_type": "FILL",
            "security_id": "XQZ-4017",
            "quantity_delta": "40" if partial_reversal else "50",
            "cost_basis_delta": "360" if partial_reversal else "450",
            "cash_delta": "-480" if partial_reversal else "-600",
            "occurred_at": "2042-05-21T15:00:00Z",
            "corrects_entry_id": "synthetic-restoration-sale",
            "correction_reason": "Synthetic late broker reversal",
            "evidence": position_evidence(
                "synthetic-late-reversal", cutoff_at=reversed_sale["knowledge_cutoff"]
            ),
        }
    )
    reopened = stress_result(migrated_settings, reversed_sale)
    assert reopened["state"] == ("BUFFER" if partial_reversal else "NORMAL")
    assert reopened["obligation"]["obligation_id"] == breach["obligation"]["obligation_id"]
    assert reopened["obligation"]["status"] == "OUTSTANDING"
    assert reopened["new_exposure_blocked"] is True


@pytest.mark.parametrize(
    ("equity", "cash", "opening", "expected"),
    [("1800", "590", "1392", "NORMAL"), ("1400", "190", "992", "BUFFER")],
)
def test_exact_thresholds_are_inclusive(
    migrated_settings: Settings, equity: str, cash: str, opening: str, expected: str
) -> None:
    authorization_id = stress_authorization(migrated_settings, shock="0.21", friction="0")
    payload = stress_payload(migrated_settings, authorization_id)
    account = payload["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = equity
    account["cash_state"].update(ledger_cash=cash, opening_ledger_cash=opening)
    result = stress_result(migrated_settings, payload)
    assert result["state"] == expected
    assert result["obligation"] is None


@pytest.mark.parametrize("origin", ["SYSTEM", "EXTERNAL"])
@pytest.mark.parametrize("restriction", ["SUSPENDED", "RISK_WARNING", "NO_BUYER"])
def test_all_origins_and_blocked_residual_positions_still_consume_stress(
    migrated_settings: Settings, origin: str, restriction: str
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_payload(migrated_settings, authorization_id)
    account = payload["stress"]["position_snapshot"]["accounts"][0]
    account["positions"][0].update(
        origin=origin, lifecycle_id="synthetic-expired-continuation", broker_sellable_quantity="0"
    )
    account["execution_restrictions"] = [
        {
            "restriction_id": "synthetic-restriction",
            "security_id": "XQZ-4017",
            "kind": restriction,
            "reason": "Synthetic execution restriction",
            "active": True,
            "evidence": position_evidence("synthetic-execution-restriction"),
        }
    ]
    result = stress_result(migrated_settings, payload)
    assert result["gross_stress_loss"] == "296.400"
    assert result["contributions"][0]["current_exposure"] == "1200"


def test_an_out_of_order_assessment_does_not_disclose_or_clear_a_future_obligation(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_payload(migrated_settings, authorization_id)
    future = next_stress_case(payload, "20")
    account = future["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = "1667.066"
    account["cash_state"].update(ledger_cash="457.066", opening_ledger_cash="1259.066")
    breach = stress_result(migrated_settings, future)
    assert breach["state"] == "HARD_BREACH"
    stale = stress_result(migrated_settings, next_stress_case(future, "19"))
    assert stale["state"] == "UNKNOWN"
    assert stale["obligation"] is None
    later = stress_result(migrated_settings, next_stress_case(future, "21"))
    assert later["obligation"]["obligation_id"] == breach["obligation"]["obligation_id"]


def test_expired_budget_preserves_stress_but_cannot_admit_new_exposure(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_payload(migrated_settings, authorization_id)
    cutoff = "2042-12-01T16:00:00Z"
    payload["knowledge_cutoff"] = cutoff
    snapshot = payload["stress"]["position_snapshot"]
    snapshot["cutoff_at"] = cutoff
    refresh_current_position_evidence(snapshot, cutoff)
    result = stress_result(migrated_settings, payload)
    assert result["state"] == "NORMAL"
    assert result["gross_stress_loss"] == "296.400"
    assert result["new_exposure_blocked"] is True
    assert result["calculation_policy"]["version_id"] == "synthetic-gross-stress-v1"
    assert result["risk_budget_version_id"] == "synthetic-risk-budget-alpha"
    assert result["budget"]["target_ratio"] == "0.14"


def test_selected_account_snapshot_does_not_need_to_include_excluded_accounts(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings, retain_excluded=True)
    payload = stress_payload(migrated_settings, authorization_id)
    payload["access_scope"]["account_ids"].append("synthetic-account-margin-2001")
    result = stress_result(migrated_settings, payload)
    assert result["state"] == "NORMAL"
    assert result["account_ids"] == ["synthetic-account-4017"]


def test_report_correction_preserves_the_original_stress_decision(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_payload(migrated_settings, authorization_id)
    original = stress_result(migrated_settings, payload)
    correction = service.correct_default_frozen_decision_case(
        FrozenDecisionCase.model_validate(payload),
        DecisionLedger.from_settings(migrated_settings, clock=GovernanceClock()),
        payload["business_identity"],
    )
    assert correction.report.model_dump(mode="json")["result"]["stress"] == original


@pytest.mark.parametrize("field", ["market_price", "total_quantity"])
def test_unknown_position_values_never_become_zero_stress(
    migrated_settings: Settings, field: str
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_payload(migrated_settings, authorization_id)
    payload["stress"]["position_snapshot"]["accounts"][0]["positions"][0][field] = None
    result = stress_result(migrated_settings, payload)
    assert result["state"] == "UNKNOWN"
    assert result["gross_stress_loss"] is None
    assert result["new_exposure_blocked"] is True
    assert result["calculation_policy"]["version_id"] == "synthetic-gross-stress-v1"
    assert result["risk_budget_version_id"] == "synthetic-risk-budget-alpha"
    assert result["budget"]["target_ratio"] == "0.14"


def test_liquidation_friction_can_exhaust_an_otherwise_positive_equity(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_payload(migrated_settings, authorization_id)
    account = payload["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = "1"
    account["cash_state"]["payable_cash"] = "3019.4"
    result = stress_result(migrated_settings, payload)
    assert result["state"] == "UNKNOWN"
    assert "STRESS_NET_EQUITY_NON_POSITIVE" in result["reasons"]
    assert result["calculation_policy"]["version_id"] == "synthetic-gross-stress-v1"
    assert result["contributions"][0]["current_exposure"] == "1200"
    assert result["gross_stress_loss"] == "296.400"
    assert result["net_liquidation_equity"] == "-19.400"


def test_policy_free_authorization_has_no_synthetic_stress_fallback(
    migrated_settings: Settings,
) -> None:
    proposal = portfolio_proposal()
    proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    executed = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings, "policy-free", portfolio_confirmation_command(proposal)
        ),
        clock=GovernanceClock(),
    )
    assert executed.report is not None and executed.report.result.portfolio is not None
    authorization = executed.report.result.portfolio.authorization
    assert authorization is not None
    result = stress_result(
        migrated_settings, stress_payload(migrated_settings, authorization.authorization_id)
    )
    assert result["state"] == "UNKNOWN"
    assert "STRESS_POLICY_REQUIRED" in result["reasons"]
    assert result["gross_stress_loss"] is None


@pytest.mark.parametrize("credit", ["correlation", "upside_expectation", "downside_probability"])
def test_unqualified_diversification_inputs_are_not_part_of_the_stress_contract(
    migrated_settings: Settings, credit: str
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_payload(migrated_settings, authorization_id)
    payload["stress"][credit] = "0.9"
    with pytest.raises(ValidationError, match=credit):
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())


def test_a_fully_reversed_sale_cannot_discharge_the_retained_obligation(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    first = stress_payload(migrated_settings, authorization_id, "reversed-sale")
    account = first["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = "1667.066"
    account["cash_state"].update(ledger_cash="457.066", opening_ledger_cash="1259.066")
    breach = stress_result(migrated_settings, first)
    corrected = next_stress_case(first, "18")
    account = corrected["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = "767.066"
    account["positions"][0]["market_price"] = "3"
    for identity, quantity, cost, cash, correction in [
        ("synthetic-reversed-sale", "-50", "-450", "600", None),
        ("synthetic-sale-reversal", "50", "450", "-600", "synthetic-reversed-sale"),
    ]:
        account["ledger_entries"].append(
            {
                "entry_id": identity,
                "entry_type": "FILL",
                "security_id": "XQZ-4017",
                "quantity_delta": quantity,
                "cost_basis_delta": cost,
                "cash_delta": cash,
                "occurred_at": "2042-05-18T15:00:00Z",
                "corrects_entry_id": correction,
                "correction_reason": "Synthetic broker reversal" if correction else None,
                "evidence": position_evidence(identity, cutoff_at=corrected["knowledge_cutoff"]),
            }
        )
    result = stress_result(migrated_settings, corrected)
    assert result["state"] == "NORMAL"
    assert result["obligation"]["obligation_id"] == breach["obligation"]["obligation_id"]
    assert result["obligation"]["status"] == "OUTSTANDING"
    assert result["new_exposure_blocked"] is True


def test_lowering_disposal_friction_requires_relaxation_evidence(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    successor = run_frozen_decision_case(
        migrated_settings,
        stress_successor(migrated_settings, authorization_id, friction="0"),
        clock=GovernanceClock("2042-05-18T16:00:00Z"),
    )
    assert successor.report is not None and successor.report.result.portfolio is not None
    assert successor.report.result.portfolio.disposition == "DENIED"
    assert successor.report.result.portfolio.reasons == (
        "RISK_BUDGET_RELAXATION_EVIDENCE_REQUIRED",
    )


def test_superseded_stress_authorization_cannot_admit_exposure(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    successor_payload = stress_successor(migrated_settings, authorization_id)
    successor_payload["portfolio"]["proposal"]["risk_budget"]["stress"] = {
        "target_ratio": "0.08",
        "hard_ratio": "0.09",
    }
    successor = run_frozen_decision_case(
        migrated_settings, successor_payload, clock=GovernanceClock("2042-05-18T16:00:00Z")
    )
    assert successor.report is not None and successor.report.result.portfolio is not None
    assert successor.report.result.portfolio.disposition == "APPROVED"
    stale = next_stress_case(stress_payload(migrated_settings, authorization_id), "19")
    result = stress_result(migrated_settings, stale)
    assert result["state"] == "UNKNOWN"
    assert result["new_exposure_blocked"] is True
    assert "STRESS_AUTHORIZATION_UNAVAILABLE" in result["reasons"]


@pytest.mark.parametrize("valuation", ["known", "missing", "nonpositive"])
def test_retained_obligation_adopts_a_stricter_current_target(
    migrated_settings: Settings,
    valuation: str,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    first = stress_payload(migrated_settings, authorization_id, "tightened-obligation")
    account = first["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = "1667.066"
    account["cash_state"].update(ledger_cash="457.066", opening_ledger_cash="1259.066")
    breach = stress_result(migrated_settings, first)
    successor_payload = stress_successor(migrated_settings, authorization_id)
    successor_payload["portfolio"]["proposal"]["risk_budget"]["stress"]["target_ratio"] = "0.08"
    successor = run_frozen_decision_case(
        migrated_settings, successor_payload, clock=GovernanceClock("2042-05-18T16:00:00Z")
    )
    assert successor.report is not None and successor.report.result.portfolio is not None
    authorization = successor.report.result.portfolio.authorization
    assert authorization is not None
    later = next_stress_case(first, "19")
    later["stress"]["authorization_id"] = authorization.authorization_id
    account = later["stress"]["position_snapshot"]["accounts"][0]
    if valuation == "missing":
        account["positions"][0]["market_price"] = None
    elif valuation == "nonpositive":
        account["account_equity"] = "1"
        account["cash_state"]["payable_cash"] = "1666.066"
    result = stress_result(migrated_settings, later)
    assert result["obligation"]["obligation_id"] == breach["obligation"]["obligation_id"]
    assert result["obligation"]["target_stress_ratio"] == "0.08"


def test_insufficient_verified_sellability_reports_a_residual_restoration_gap(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_payload(migrated_settings, authorization_id, "partial-sellability")
    account = payload["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = "1667.066"
    account["cash_state"].update(ledger_cash="457.066", opening_ledger_cash="1259.066")
    account["positions"][0]["broker_sellable_quantity"] = "1"
    result = stress_result(migrated_settings, payload)
    assert result["state"] == "HARD_BREACH"
    assert result["execution_blocked"] is True
    assert result["residual_restoration_gap"] == "62.90276"
    assert result["obligation"]["status"] == "OUTSTANDING"


def test_stress_policy_identity_cannot_be_redefined_by_a_successor(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_successor(migrated_settings, authorization_id)
    payload["portfolio"]["proposal"]["risk_budget"]["stress_calculation"][
        "disposal_friction_ratio"
    ] = "0.025"
    executed = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-18T16:00:00Z")
    )
    assert executed.report is not None and executed.report.result.portfolio is not None
    assert executed.report.result.portfolio.reasons == ("STRESS_CALCULATION_POLICY_REDEFINED",)
    assert executed.report.result.portfolio.disposition == "DENIED"


def test_stress_policy_must_be_registered_before_user_confirmation(
    migrated_settings: Settings,
) -> None:
    authorization_id = stress_authorization(migrated_settings)
    payload = stress_successor(migrated_settings, authorization_id)
    payload["portfolio"]["proposal"]["risk_budget"]["stress_calculation"].update(
        version_id="synthetic-late-policy",
        registered_at="2042-05-18T15:00:00Z",
    )
    with pytest.raises(ValidationError, match="stress policy must predate confirmation"):
        FrozenDecisionCase.model_validate(payload)


@pytest.mark.parametrize("future_restoration", [False, True])
def test_valid_relaxation_evidence_cannot_erase_an_unfinished_stress_obligation(
    migrated_settings: Settings,
    future_restoration: bool,
) -> None:
    authorization_id = stress_authorization(migrated_settings, cash_obligations=False)
    payload = stress_payload(migrated_settings, authorization_id, "relaxation-pending")
    account = payload["stress"]["position_snapshot"]["accounts"][0]
    account["account_equity"] = "1667.066"
    account["cash_state"].update(ledger_cash="457.066", opening_ledger_cash="1259.066")
    assert stress_result(migrated_settings, payload)["obligation"]["status"] == "OUTSTANDING"
    if future_restoration:
        future = next_stress_case(payload, "18")
        cutoff = "2042-06-18T16:00:00Z"
        future["knowledge_cutoff"] = cutoff
        snapshot = future["stress"]["position_snapshot"]
        snapshot["cutoff_at"] = cutoff
        refresh_current_position_evidence(snapshot, cutoff)
        account = snapshot["accounts"][0]
        account["positions"][0].update(
            total_quantity="50", broker_sellable_quantity="30", reported_cost_basis="450"
        )
        account["cash_state"]["ledger_cash"] = "1057.066"
        account["ledger_entries"].append(
            {
                "entry_id": "synthetic-future-restoration",
                "entry_type": "FILL",
                "security_id": "XQZ-4017",
                "quantity_delta": "-50",
                "cost_basis_delta": "-450",
                "cash_delta": "600",
                "occurred_at": "2042-06-18T15:00:00Z",
                "evidence": position_evidence("synthetic-future-restoration", cutoff_at=cutoff),
            }
        )
        assert stress_result(migrated_settings, future)["obligation"]["status"] == "SATISFIED"
    proposal = relaxed_portfolio_proposal()
    for name in ("snapshot", "activation_snapshot"):
        proposal[name]["accounts"] = proposal[name]["accounts"][:1]
    original = stress_proposal()["risk_budget"]
    for name in ("downside_grid", "stress_calculation"):
        proposal["risk_budget"][name] = original[name]
    proposal["risk_budget"]["stress"]["target_ratio"] = "0.16"
    command = portfolio_confirmation_command(proposal, previous_authorization_id=authorization_id)
    command["confirmation"]["relaxation_evidence"] = relaxation_evidence(authorization_id)
    executed = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(migrated_settings, "relaxation-pending-successor", command),
        clock=GovernanceClock("2042-06-17T16:00:00Z"),
    )
    assert executed.report is not None and executed.report.result.portfolio is not None
    assert executed.report.result.portfolio.disposition == "DENIED"
    assert executed.report.result.portfolio.reasons == (
        "RISK_BUDGET_RELAXATION_OBLIGATIONS_UNRESOLVED",
    )


def test_full_selected_accounts_are_aggregated_without_issuer_diversification(
    migrated_settings: Settings,
) -> None:
    proposal = stress_proposal()
    proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    second = deepcopy(proposal["snapshot"]["accounts"][0])
    second["account_id"] = "synthetic-account-8029"
    proposal["snapshot"]["accounts"].append(second)
    proposal["snapshot"]["selected_account_ids"].append(second["account_id"])
    authorized = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings, "stress-two-accounts", portfolio_confirmation_command(proposal)
        ),
        clock=GovernanceClock(),
    )
    assert authorized.report is not None and authorized.report.result.portfolio is not None
    authorization = authorized.report.result.portfolio.authorization
    assert authorization is not None
    payload = stress_payload(migrated_settings, authorization.authorization_id, "two-accounts")
    payload["stress"]["position_snapshot"] = position_snapshot_command()
    payload["access_scope"]["account_ids"].append(second["account_id"])
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        result = stress_result(migrated_settings, payload)
    assert Decimal(result["gross_stress_loss"]) == Decimal("444.6")
    assert Decimal(result["net_liquidation_equity"]) == Decimal("2079.4")
    assert len(result["contributions"]) == 2
    assert result["state"] == "HARD_BREACH"
    missing = next_stress_case(payload, "18")
    missing["stress"]["position_snapshot"]["accounts"].pop()
    denied = stress_result(migrated_settings, missing)
    assert denied["state"] == "UNKNOWN"
    assert "STRESS_ACCOUNT_SCOPE_MISMATCH" in denied["reasons"]
    assert denied["new_exposure_blocked"] is True


def test_stress_requires_an_authorized_not_shadow_portfolio(migrated_settings: Settings) -> None:
    result = stress_result(
        migrated_settings, stress_payload(migrated_settings, "synthetic-unconfirmed-shadow")
    )
    assert result["state"] == "UNKNOWN"
    assert result["calculation_policy"] is None
    assert result["obligation"] is None
    assert "STRESS_AUTHORIZATION_UNAVAILABLE" in result["reasons"]
