from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
from test_drawdown_protection import drawdown_case, observe, opening_payload
from test_portfolio_authorization import (
    portfolio_case_payload,
    portfolio_confirmation_command,
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

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.service import correct_default_frozen_decision_case


def test_correction_preserves_capital_facts(migrated_settings: Settings) -> None:
    payload = opening_payload(migrated_settings)
    original = run_frozen_decision_case(migrated_settings, payload)
    assert original.report is not None
    case = FrozenDecisionCase.model_validate(payload)
    correction = correct_default_frozen_decision_case(
        case,
        DecisionLedger.from_settings(
            migrated_settings,
            clock=GovernanceClock("2042-05-18T16:01:00Z"),
        ),
        case.business_identity,
    )
    assert correction.report.result.drawdown == original.report.result.drawdown


@pytest.mark.parametrize("threshold", ["0.08", "0.06"])
def test_effective_tightening_cannot_be_bypassed_with_predecessor_budget(
    migrated_settings: Settings,
    threshold: str,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    proposal = portfolio_proposal()
    proposal["cash_obligations"] = []
    proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-tightening-selection",
        cutoff="2042-05-18T16:00:00Z",
    )
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-tightening-effective",
        cutoff="2042-05-19T16:00:00Z",
    )
    proposal["risk_budget"].update(
        version_id="synthetic-capital-tightened",
        effective_at="2042-05-19T16:00:00Z",
        expires_at="2042-11-19T16:00:00Z",
    )
    proposal["risk_budget"]["drawdown"]["caution_ratio"] = threshold
    tighter = run_frozen_decision_case(
        settings,
        portfolio_case_payload(
            settings,
            "capital-tightened",
            portfolio_confirmation_command(
                proposal,
                previous_authorization_id=payload["drawdown"]["authorization_id"],
            ),
        ),
        clock=GovernanceClock("2042-05-19T16:01:00Z"),
    )
    assert tighter.report is not None and tighter.report.result.portfolio is not None
    authorization = tighter.report.result.portfolio.authorization
    assert authorization is not None, tighter.report.result.portfolio.reasons
    observed = observe(
        settings,
        payload["drawdown"],
        opened.report,
        "old-budget-request",
        "2042-05-20T16:00:00Z",
        equity="1170",
    )
    outcome = observed.result.drawdown
    assert outcome is not None and outcome.state is not None
    assert outcome.state.risk_state == "CAUTION"
    assert outcome.state.authorization_id == authorization.authorization_id
    assert outcome.state.new_exposure_blocked


def cash_snapshot(
    settings: Settings,
    identity: str,
    cutoff: str,
    *,
    equity: str,
    transfers: list[dict[str, Any]],
    equity_expiry: str | None = None,
) -> tuple[str, dict[str, Any]]:
    snapshot = position_snapshot_command()
    snapshot.update(snapshot_id=f"synthetic-{identity}", cutoff_at=cutoff)
    snapshot["accounts"] = snapshot["accounts"][:1]
    snapshot["annotations"] = []
    refresh_current_position_evidence(snapshot, cutoff)
    account = snapshot["accounts"][0]
    account["account_equity_evidence"]["expires_at"] = equity_expiry
    delta = sum((Decimal(item["cash_delta"]) for item in transfers), Decimal(0))
    account["account_equity"] = str(Decimal(equity) + 10)
    account["positions"][0]["market_price"] = str((Decimal(equity) - 100 - delta) / 100)
    for field in ("ledger_cash", "trading_cash", "transferable_cash"):
        account["cash_state"][field] = str(Decimal(account["cash_state"][field]) + delta)
    account["ledger_entries"].extend(transfers)
    execution = run_frozen_decision_case(
        settings,
        position_case_payload(settings, identity, snapshot),
    )
    assert execution.report is not None and execution.report.result.position is not None
    assert execution.report.result.position.disposition == "RECONCILED"
    return execution.report.event_id, snapshot["snapshot_evidence"]


def transfer(identity: str, occurred: str, cutoff: str) -> dict[str, Any]:
    return {
        "entry_id": f"synthetic-{identity}",
        "entry_type": "TRANSFER_IN",
        "security_id": None,
        "quantity_delta": "0",
        "cost_basis_delta": "0",
        "cash_delta": "400",
        "occurred_at": occurred,
        "evidence": position_evidence(f"synthetic-{identity}", cutoff_at=cutoff),
    }


@pytest.mark.parametrize("fresh_proof", [True, False])
def test_multiple_flows_require_sequential_qualified_valuations(
    migrated_settings: Settings,
    fresh_proof: bool,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    cutoff = "2042-05-19T16:00:00Z"
    transfers = [
        transfer("first-flow", "2042-05-18T15:00:00Z", "2042-05-18T16:00:00Z"),
        transfer("second-flow", "2042-05-19T15:00:00Z", cutoff),
    ]
    second_valuation = deepcopy(payload["drawdown"]["valuation"])
    if fresh_proof:
        first_event, first_evidence = cash_snapshot(
            settings,
            "first-flow-qualified",
            "2042-05-18T16:00:00Z",
            equity="1700",
            transfers=transfers[:1],
        )
        second_valuation.update(position_event_id=first_event, evidence=first_evidence)
    event_id, evidence = cash_snapshot(
        settings,
        "multiple-flows",
        cutoff,
        equity="2100",
        transfers=transfers,
    )
    command = deepcopy(payload["drawdown"])
    command.update(
        operation="OBSERVE",
        previous_decision_id=opened.report.event_id,
        cutoff_at=cutoff,
        capital_flows=[
            {
                "flow_id": item["entry_id"],
                "kind": "EXTERNAL",
                "occurred_at": item["occurred_at"],
                "before_valuation": deepcopy(command["valuation"]),
                "ledger_keys": [
                    {
                        "account_id": "synthetic-account-4017",
                        "entry_id": item["entry_id"],
                    }
                ],
            }
            for item in transfers
        ],
    )
    command["capital_flows"][1]["before_valuation"] = second_valuation
    command["valuation"].update(position_event_id=event_id, evidence=evidence)
    result = run_frozen_decision_case(
        settings, drawdown_case(settings, "stale-flow-proof", command)
    )
    assert result.report is not None and result.report.result.drawdown is not None
    outcome = result.report.result.drawdown
    assert outcome.disposition == ("ACCEPTED" if fresh_proof else "UNKNOWN")
    assert outcome.state is not None
    assert outcome.state.units == Decimal("2100" if fresh_proof else "1300")
    assert outcome.state.maximum_drawdown == 0
    assert outcome.state.new_exposure_blocked is (not fresh_proof)


def test_intervening_qualified_breach_resets_recovery(migrated_settings: Settings) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    prior = observe(
        settings,
        payload["drawdown"],
        opened.report,
        "interval-caution",
        "2042-05-18T16:00:00Z",
        equity="1157",
    )
    prior = observe(
        settings,
        payload["drawdown"],
        prior,
        "interval-good-close",
        "2042-05-20T16:00:00Z",
        equity="1250",
        session=6101,
    )
    pre_event, pre_evidence = cash_snapshot(
        settings,
        "interval-pre-flow",
        "2042-05-20T17:00:00Z",
        equity="1157",
        transfers=[],
    )
    cutoff = "2042-05-21T16:00:00Z"
    flow = transfer("interval-flow", "2042-05-21T15:00:00Z", cutoff)
    post_event, post_evidence = cash_snapshot(
        settings,
        "interval-post-flow",
        cutoff,
        equity="1700",
        transfers=[flow],
    )
    command = deepcopy(payload["drawdown"])
    command.update(
        operation="OBSERVE",
        previous_decision_id=prior.event_id,
        cutoff_at=cutoff,
        market_session_ordinal=6102,
        other_risk_gate={"hard_gate_active": False, "evidence": post_evidence},
        capital_flows=[
            {
                "flow_id": flow["entry_id"],
                "kind": "EXTERNAL",
                "occurred_at": flow["occurred_at"],
                "before_valuation": {
                    "position_event_id": pre_event,
                    "liquidation_cost": "10",
                    "evidence": pre_evidence,
                },
                "ledger_keys": [
                    {
                        "account_id": "synthetic-account-4017",
                        "entry_id": flow["entry_id"],
                    }
                ],
            }
        ],
    )
    command["valuation"].update(position_event_id=post_event, evidence=post_evidence)
    result = run_frozen_decision_case(settings, drawdown_case(settings, "interval-breach", command))
    assert result.report is not None and result.report.result.drawdown is not None
    state = result.report.result.drawdown.state
    assert state is not None
    assert state.risk_state == "CAUTION"
    assert state.recovery_sessions == 0


def test_late_flow_proof_resumes_from_last_qualified_accounting_cutoff(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    unknown = observe(
        settings,
        payload["drawdown"],
        opened.report,
        "unknown-accounting",
        "2042-05-18T16:00:00Z",
        equity="1300",
        unknown=True,
    )
    cutoff = "2042-05-19T16:00:00Z"
    flow = transfer("recovered-flow", "2042-05-18T15:00:00Z", "2042-05-18T16:00:00Z")
    event_id, evidence = cash_snapshot(
        settings,
        "recovered-accounting",
        cutoff,
        equity="1700",
        transfers=[flow],
    )
    command = deepcopy(payload["drawdown"])
    command.update(
        operation="OBSERVE",
        previous_decision_id=unknown.event_id,
        cutoff_at=cutoff,
        capital_flows=[
            {
                "flow_id": flow["entry_id"],
                "kind": "EXTERNAL",
                "occurred_at": flow["occurred_at"],
                "before_valuation": deepcopy(command["valuation"]),
                "ledger_keys": [
                    {
                        "account_id": "synthetic-account-4017",
                        "entry_id": flow["entry_id"],
                    }
                ],
            }
        ],
    )
    command["valuation"].update(position_event_id=event_id, evidence=evidence)
    result = run_frozen_decision_case(
        settings, drawdown_case(settings, "recovered-accounting", command)
    )
    assert result.report is not None and result.report.result.drawdown is not None
    outcome = result.report.result.drawdown
    assert outcome.disposition == "ACCEPTED", outcome.reasons
    assert outcome.state is not None and outcome.state.unit_nav == 1


@pytest.mark.parametrize("pre_flow", [False, True])
def test_reconciled_foreign_currency_is_not_comparable_capital_evidence(
    migrated_settings: Settings,
    pre_flow: bool,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    snapshot = position_snapshot_command()
    snapshot["accounts"] = snapshot["accounts"][:1]
    snapshot["annotations"] = []
    snapshot.update(
        snapshot_id="synthetic-other-currency",
        cutoff_at="2042-05-18T16:00:00Z",
        valuation_currency="YSP",
    )
    snapshot["accounts"][0]["currency"] = "YSP"
    refresh_current_position_evidence(snapshot, snapshot["cutoff_at"])
    position = run_frozen_decision_case(
        settings,
        position_case_payload(settings, "other-currency", snapshot),
    )
    assert position.report is not None
    command = deepcopy(payload["drawdown"])
    command.update(
        operation="OBSERVE",
        previous_decision_id=opened.report.event_id,
        cutoff_at=snapshot["cutoff_at"],
    )
    command["valuation"].update(
        position_event_id=position.report.event_id,
        evidence=snapshot["snapshot_evidence"],
    )
    if pre_flow:
        cutoff = "2042-05-19T16:00:00Z"
        flow = transfer("other-currency-flow", "2042-05-19T15:00:00Z", cutoff)
        event_id, evidence = cash_snapshot(
            settings,
            "other-currency-after-flow",
            cutoff,
            equity="1700",
            transfers=[flow],
        )
        command.update(
            cutoff_at=cutoff,
            capital_flows=[
                {
                    "flow_id": flow["entry_id"],
                    "kind": "EXTERNAL",
                    "occurred_at": flow["occurred_at"],
                    "before_valuation": deepcopy(command["valuation"]),
                    "ledger_keys": [
                        {
                            "account_id": "synthetic-account-4017",
                            "entry_id": flow["entry_id"],
                        }
                    ],
                }
            ],
        )
        command["valuation"].update(position_event_id=event_id, evidence=evidence)
    observed = run_frozen_decision_case(
        settings,
        drawdown_case(settings, "other-currency", command),
    )
    assert observed.report is not None and observed.report.result.drawdown is not None
    assert observed.report.result.drawdown.disposition == "UNKNOWN"
    state = observed.report.result.drawdown.state
    assert state is not None and state.new_exposure_blocked
    assert state.units == 1300 and state.high_water_nav == 1
