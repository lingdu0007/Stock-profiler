from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest
from pydantic import ValidationError
from test_portfolio_authorization import (
    portfolio_case_payload,
    portfolio_confirmation_command,
    portfolio_proposal,
)
from test_position_state_reconciliation import (
    position_case_payload,
    position_evidence,
    position_snapshot_command,
    refresh_current_position_evidence,
)
from test_scoped_qualification import GovernanceClock

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase


def stress_authorization(
    settings: Settings,
    *,
    shock: str = "0.23",
    friction: str = "0.017",
    retain_excluded: bool = False,
) -> str:
    proposal = portfolio_proposal()
    if not retain_excluded:
        proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    proposal["risk_budget"]["downside_grid"] = ["0.04", "0.09", shock]
    proposal["risk_budget"]["stress_calculation"] = {
        "contract_version": "1.0.0",
        "version_id": "synthetic-gross-stress-v1",
        "horizon_market_days": 20,
        "shock_ratio": shock,
        "disposal_friction_ratio": friction,
        "registered_at": "2042-05-16T16:00:00Z",
    }
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


def test_restoration_survives_unknown_evidence_and_requires_a_reconciled_fill(
    migrated_settings: Settings,
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
