from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
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

from stock_profiler.adapters.authentication.passkeys import PasskeyAuthenticator
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints import cli
from stock_profiler.entrypoints.http.app import create_app
from stock_profiler.modules.decision_cases.domain import FormalReport, load_frozen_decision_case


def drawdown_case(
    settings: Settings,
    identity: str,
    command: dict[str, Any],
    *,
    account_ids: list[str] | None = None,
) -> dict[str, Any]:
    payload = load_frozen_decision_case(settings).model_dump(mode="json")
    payload["case_id"] = f"synthetic-drawdown-{identity}"
    payload["business_identity"] = f"synthetic:drawdown:{identity}"
    payload["version_bundle"].update(
        case_contract_version="drawdown.1.0.0",
        host_contract_version="drawdown.1.0.0",
        report_projection_contract_version="drawdown.1.0.0",
        agent_definition_version="2.0.0",
    )
    payload["agent_definition"]["version"] = "2.0.0"
    payload["access_scope"] = {
        "contract_version": "1.0.0",
        "user_id": "stock-profiler-single-user",
        "account_ids": account_ids or ["synthetic-account-4017"],
        "visibility": "USER",
    }
    payload["input"]["account"]["account_id"] = "synthetic-account-4017"
    payload["knowledge_cutoff"] = command["cutoff_at"]
    payload["drawdown"] = command
    return payload


def opening_payload(settings: Settings, *, combined: bool = False) -> dict[str, Any]:
    proposal = portfolio_proposal()
    proposal["cash_obligations"] = []
    proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    if combined:
        other = deepcopy(portfolio_fixture()["next_cash_account"])
        other["captured_at"] = proposal["snapshot"]["cutoff_at"]
        proposal["snapshot"]["accounts"].append(other)
        proposal["snapshot"]["selected_account_ids"].append(other["account_id"])
    authorization = run_frozen_decision_case(
        settings,
        portfolio_case_payload(
            settings, "drawdown-capital", portfolio_confirmation_command(proposal)
        ),
        clock=GovernanceClock(),
    )
    assert authorization.report is not None
    assert authorization.report.result.portfolio is not None
    capital = authorization.report.result.portfolio.authorization
    assert capital is not None, authorization.report.result.portfolio.reasons
    snapshot = position_snapshot_command()
    snapshot["accounts"] = snapshot["accounts"][: 2 if combined else 1]
    snapshot["annotations"] = []
    position = run_frozen_decision_case(
        settings, position_case_payload(settings, "drawdown-opening", snapshot)
    )
    assert position.report is not None
    payload = drawdown_case(
        settings,
        "opening",
        {
            "operation": "OPEN",
            "contract_version": "1.0.0",
            "portfolio_id": capital.proposal.portfolio_id,
            "authorization_id": capital.authorization_id,
            "epoch_id": "synthetic-capital-epoch-alpha",
            "previous_decision_id": None,
            "cutoff_at": snapshot["cutoff_at"],
            "valuation": {
                "position_event_id": position.report.event_id,
                "liquidation_cost": "10",
                "evidence": snapshot["snapshot_evidence"],
            },
            "policy": {
                "synthetic": True,
                "generator_version": "drawdown-demonstration/1",
                "seed": 7381,
                "version_id": "synthetic-drawdown-policy-alpha",
                "defensive_exposure_ratio": "0.27",
                "caution_recovery_ratio": "0.07",
                "defensive_recovery_ratio": "0.09",
                "caution_recovery_sessions": 2,
                "defensive_recovery_sessions": 3,
                "cooling_sessions": 4,
                "market_calendar_version_id": "synthetic-market-calendar-v1",
            },
        },
        account_ids=[item["account_id"] for item in snapshot["accounts"]],
    )
    return payload


@pytest.mark.parametrize("declared", [True, False])
def test_internal_transfer_preserves_units_or_fails_closed_when_unexplained(
    migrated_settings: Settings,
    declared: bool,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings, combined=True)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    snapshot = position_snapshot_command()
    snapshot["annotations"] = []
    snapshot.update(snapshot_id="synthetic-internal-transfer", cutoff_at="2042-05-18T16:00:00Z")
    refresh_current_position_evidence(snapshot, snapshot["cutoff_at"])
    keys = []
    for account, delta in zip(snapshot["accounts"], (-40, 40), strict=True):
        account["account_equity"] = str(Decimal(account["account_equity"]) + delta)
        for field in ("ledger_cash", "trading_cash", "transferable_cash"):
            account["cash_state"][field] = str(Decimal(account["cash_state"][field]) + delta)
        key = {"account_id": account["account_id"], "entry_id": "synthetic-internal-flow"}
        keys.append(key)
        account["ledger_entries"].append(
            {
                "entry_id": key["entry_id"],
                "entry_type": "TRANSFER_IN" if delta > 0 else "TRANSFER_OUT",
                "security_id": None,
                "quantity_delta": "0",
                "cost_basis_delta": "0",
                "cash_delta": str(delta),
                "occurred_at": "2042-05-18T15:00:00Z",
                "evidence": position_evidence(
                    "synthetic-internal", cutoff_at=snapshot["cutoff_at"]
                ),
            }
        )
    position = run_frozen_decision_case(
        settings, position_case_payload(settings, "internal-transfer", snapshot)
    )
    assert position.report is not None
    command = deepcopy(payload["drawdown"])
    command.update(
        operation="OBSERVE",
        previous_decision_id=opened.report.event_id,
        cutoff_at=snapshot["cutoff_at"],
        capital_flows=[
            {
                "flow_id": "synthetic-internal-flow",
                "kind": "INTERNAL",
                "occurred_at": "2042-05-18T15:00:00Z",
                "before_valuation": deepcopy(command["valuation"]),
                "ledger_keys": keys,
            }
        ]
        if declared
        else [],
    )
    command["valuation"].update(
        position_event_id=position.report.event_id,
        evidence=snapshot["snapshot_evidence"],
    )
    execution = run_frozen_decision_case(
        settings,
        drawdown_case(
            settings,
            "internal-transfer",
            command,
            account_ids=payload["access_scope"]["account_ids"],
        ),
    )
    assert execution.report is not None
    outcome = execution.report.result.model_dump(mode="json")["drawdown"]
    state = outcome["state"]
    assert state is not None, outcome["reasons"]
    assert Decimal(state["units"]) == 2100
    assert Decimal(state["high_water_nav"]) == 1
    assert outcome["disposition"] == ("ACCEPTED" if declared else "UNKNOWN")
    assert state["new_exposure_blocked"] is (not declared)
    assert state["unit_nav"] == ("1" if declared else None)


def test_pre_flow_qualified_trough_is_retained_when_final_nav_rebounds(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    before = position_snapshot_command()
    before["accounts"] = before["accounts"][:1]
    before["annotations"] = []
    before.update(snapshot_id="synthetic-pre-flow-trough", cutoff_at="2042-05-18T16:00:00Z")
    refresh_current_position_evidence(before, before["cutoff_at"])
    before["accounts"][0]["account_equity"] = "1037"
    before["accounts"][0]["positions"][0]["market_price"] = "9.27"
    pre = run_frozen_decision_case(
        settings, position_case_payload(settings, "pre-flow-trough", before)
    )
    assert pre.report is not None
    after = deepcopy(before)
    after.update(snapshot_id="synthetic-after-flow-rebound", cutoff_at="2042-05-19T16:00:00Z")
    refresh_current_position_evidence(after, after["cutoff_at"])
    account = after["accounts"][0]
    account["account_equity"] = "1710"
    account["positions"][0]["market_price"] = "12"
    for field in ("ledger_cash", "trading_cash", "transferable_cash"):
        account["cash_state"][field] = str(Decimal(account["cash_state"][field]) + 400)
    account["ledger_entries"].append(
        {
            "entry_id": "synthetic-trough-flow",
            "entry_type": "TRANSFER_IN",
            "security_id": None,
            "quantity_delta": "0",
            "cost_basis_delta": "0",
            "cash_delta": "400",
            "occurred_at": "2042-05-19T15:00:00Z",
            "evidence": position_evidence("synthetic-trough-flow", cutoff_at=after["cutoff_at"]),
        }
    )
    post = run_frozen_decision_case(
        settings, position_case_payload(settings, "after-flow-rebound", after)
    )
    assert post.report is not None
    command = deepcopy(payload["drawdown"])
    command.update(
        operation="OBSERVE",
        cutoff_at=after["cutoff_at"],
        previous_decision_id=opened.report.event_id,
        capital_flows=[
            {
                "flow_id": "synthetic-trough-flow",
                "kind": "EXTERNAL",
                "occurred_at": "2042-05-19T15:00:00Z",
                "before_valuation": {
                    "position_event_id": pre.report.event_id,
                    "liquidation_cost": "10",
                    "evidence": before["snapshot_evidence"],
                },
                "ledger_keys": [
                    {
                        "account_id": account["account_id"],
                        "entry_id": "synthetic-trough-flow",
                    }
                ],
            }
        ],
    )
    command["valuation"].update(
        position_event_id=post.report.event_id,
        evidence=after["snapshot_evidence"],
    )
    result = run_frozen_decision_case(settings, drawdown_case(settings, "flow-rebound", command))
    assert result.report is not None
    state = result.report.result.model_dump(mode="json")["drawdown"]["state"]
    assert state["risk_state"] == "PRESERVATION"
    assert Decimal(state["maximum_drawdown"]) == Decimal("0.21")
    assert Decimal(state["current_drawdown"]) < Decimal("0.11")


def test_capital_epoch_uses_committed_authorization_and_position_and_replays(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    result = opened.report.result.model_dump(mode="json")["drawdown"]
    assert result["disposition"] == "ACCEPTED"
    state = result["state"]
    assert state["epoch_id"] == "synthetic-capital-epoch-alpha"
    assert state["risk_state"] == "NORMAL"
    assert Decimal(state["net_liquidation_equity"]) == Decimal("1300")
    assert Decimal(state["units"]) == Decimal("1300")
    assert Decimal(state["unit_nav"]) == Decimal("1")
    assert Decimal(state["high_water_nav"]) == Decimal("1")
    assert state["new_exposure_blocked"] is False
    repeated = run_frozen_decision_case(settings, payload)
    assert repeated.report == opened.report
    assert repeated.framework_run_id == opened.framework_run_id


@pytest.mark.parametrize(
    ("equity", "risk_state", "blocked", "limit"),
    [
        ("1157.00013", "NORMAL", False, None),
        ("1157", "CAUTION", True, None),
        ("1092", "DEFENSIVE", True, "0.27"),
        ("1027", "PRESERVATION", True, "0"),
        ("900", "PRESERVATION", True, "0"),
    ],
)
def test_single_observation_escalates_at_exact_boundaries_without_assuming_execution(
    migrated_settings: Settings,
    equity: str,
    risk_state: str,
    blocked: bool,
    limit: str | None,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    command = payload["drawdown"]
    snapshot = position_snapshot_command()
    snapshot["accounts"] = snapshot["accounts"][:1]
    snapshot["annotations"] = []
    snapshot["cutoff_at"] = "2042-05-18T16:00:00Z"
    snapshot["snapshot_id"] = "synthetic-drawdown-next-position"
    refresh_current_position_evidence(snapshot, snapshot["cutoff_at"])
    account = snapshot["accounts"][0]
    account["account_equity"] = str(Decimal(equity) + 10)
    account["positions"][0]["market_price"] = str((Decimal(equity) - 100) / 100)
    position = run_frozen_decision_case(
        settings, position_case_payload(settings, "drawdown-next-position", snapshot)
    )
    assert position.report is not None
    command.update(
        operation="OBSERVE",
        previous_decision_id=opened.report.event_id,
        cutoff_at=snapshot["cutoff_at"],
    )
    command["valuation"].update(
        position_event_id=position.report.event_id,
        evidence=snapshot["snapshot_evidence"],
    )
    observed = run_frozen_decision_case(settings, drawdown_case(settings, "next", command))
    assert observed.report is not None
    outcome = observed.report.result.model_dump(mode="json")["drawdown"]
    assert outcome["disposition"] == "ACCEPTED"
    state = outcome["state"]
    assert state["risk_state"] == risk_state, "PROTECTION_CONTRACT_CAPITAL_PRESERVATION_GATE"
    assert state["new_exposure_blocked"] is blocked
    assert state["stock_exposure_limit"] == limit
    assert state["risk_direction"] == (
        "EXIT" if risk_state == "PRESERVATION" else "REDUCE" if risk_state == "DEFENSIVE" else None
    )
    assert state["execution_blocked"] is (risk_state in {"DEFENSIVE", "PRESERVATION"})
    assert Decimal(state["units"]) == Decimal("1300")
    assert Decimal(state["high_water_nav"]) == Decimal("1")
    assert Decimal(state["current_stock_exposure"]) == Decimal(equity) - 100
    assert Decimal(state["maximum_drawdown"]) == Decimal(state["current_drawdown"])


def observe(
    settings: Settings,
    original: dict[str, Any],
    previous: FormalReport,
    identity: str,
    cutoff: str,
    *,
    equity: str,
    quantity: int = 100,
    unknown: bool = False,
    session: int | None = None,
    hard_gate: bool = False,
    operation: str = "OBSERVE",
    settled: bool = False,
    extra_ledger_entries: tuple[dict[str, Any], ...] = (),
) -> FormalReport:
    snapshot = position_snapshot_command()
    snapshot.update(snapshot_id=f"synthetic-drawdown-position-{identity}", cutoff_at=cutoff)
    snapshot["accounts"] = snapshot["accounts"][:1]
    snapshot["annotations"] = []
    refresh_current_position_evidence(snapshot, cutoff)
    account = snapshot["accounts"][0]
    cash = Decimal(100 + (100 - quantity) * 12)
    if settled:
        cash += 10
        account["cash_state"]["receivable_cash"] = "0"
        account["ledger_entries"].append(
            {
                "entry_id": "synthetic-drawdown-receivable-settlement",
                "entry_type": "CORPORATE_ACTION",
                "security_id": account["positions"][0]["security_id"],
                "quantity_delta": "0",
                "cost_basis_delta": "0",
                "cash_delta": "10",
                "occurred_at": "2042-05-18T15:00:00Z",
                "evidence": position_evidence(
                    "synthetic-settlement", cutoff_at="2042-05-18T16:00:00Z"
                ),
            }
        )
    extra_cash = sum((Decimal(entry["cash_delta"]) for entry in extra_ledger_entries), Decimal(0))
    cash += extra_cash
    for field in ("ledger_cash", "trading_cash", "transferable_cash"):
        account["cash_state"][field] = str(Decimal(account["cash_state"][field]) + extra_cash)
    account["account_equity"] = str(Decimal(equity) + 10)
    holding = account["positions"][0]
    holding["market_price"] = str((Decimal(equity) - cash) / quantity) if quantity else "12"
    if quantity != 100:
        holding.update(
            total_quantity=str(quantity),
            broker_sellable_quantity=str(quantity),
            unsettled_quantity="0",
            frozen_quantity="0",
            restricted_quantity="0",
            open_sell_order_quantity="0",
            reported_cost_basis=str(9 * quantity),
        )
        account["open_orders"] = []
        account["execution_restrictions"] = []
        account["cash_state"].update(
            ledger_cash=str(cash),
            trading_cash=str(cash),
            transferable_cash=str(cash),
            frozen_cash="0",
        )
        account["ledger_entries"].append(
            {
                "entry_id": "synthetic-drawdown-disposal",
                "entry_type": "FILL",
                "security_id": holding["security_id"],
                "quantity_delta": str(quantity - 100),
                "cost_basis_delta": str((quantity - 100) * 9),
                "cash_delta": str((100 - quantity) * 12),
                "occurred_at": "2042-05-18T15:00:00Z",
                "evidence": position_evidence(
                    "synthetic-drawdown-disposal", cutoff_at="2042-05-18T16:00:00Z"
                ),
            }
        )
    account["ledger_entries"].extend(deepcopy(extra_ledger_entries))
    position = run_frozen_decision_case(
        settings, position_case_payload(settings, f"drawdown-{identity}", snapshot)
    )
    assert position.report is not None
    assert position.report.result.position is not None
    assert position.report.result.position.disposition == "RECONCILED", (
        position.report.result.position.reasons
    )
    command = deepcopy(original)
    command.update(
        operation=operation,
        previous_decision_id=previous.event_id,
        cutoff_at=cutoff,
        market_session_ordinal=session,
        other_risk_gate={
            "hard_gate_active": hard_gate,
            "evidence": snapshot["snapshot_evidence"],
        },
    )
    command["valuation"].update(
        position_event_id=position.report.event_id,
        liquidation_cost=None if unknown else "10",
        evidence=snapshot["snapshot_evidence"],
    )
    execution = run_frozen_decision_case(settings, drawdown_case(settings, identity, command))
    assert execution.report is not None
    return execution.report


def test_caution_recovery_needs_consecutive_closes_and_resets_for_unknown_evidence(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    command = payload["drawdown"]
    prior = observe(
        settings, command, opened.report, "caution", "2042-05-18T16:00:00Z", equity="1157"
    )
    for identity, cutoff, session, unknown, expected_count, expected_risk in (
        ("good-1", "2042-05-20T16:00:00Z", 6101, False, 1, "CAUTION"),
        ("gap", "2042-05-21T16:00:00Z", 6102, True, 0, "CAUTION"),
        ("good-2", "2042-05-22T16:00:00Z", 6103, False, 1, "CAUTION"),
        ("good-3", "2042-05-25T16:00:00Z", 6104, False, 0, "NORMAL"),
    ):
        prior = observe(
            settings,
            command,
            prior,
            identity,
            cutoff,
            equity="1250",
            unknown=unknown,
            session=session,
        )
        outcome = prior.result.model_dump(mode="json")["drawdown"]
        state = outcome["state"]
        assert state["risk_state"] == expected_risk
        assert state["recovery_sessions"] == expected_count
        assert state["new_exposure_blocked"] is (expected_risk != "NORMAL")
        if unknown:
            assert outcome["disposition"] == "UNKNOWN"
            assert state["unit_nav"] is None
            assert state["current_drawdown"] is None
            assert Decimal(state["high_water_nav"]) == Decimal("1")


def test_defensive_recovery_requires_actual_exposure_and_cannot_skip_caution(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    command = payload["drawdown"]
    prior = observe(
        settings, command, opened.report, "defensive", "2042-05-18T16:00:00Z", equity="1092"
    )
    prior = observe(
        settings, command, prior, "rebound", "2042-05-20T16:00:00Z", equity="1250", session=6101
    )
    assert prior.result.model_dump(mode="json")["drawdown"]["state"]["recovery_sessions"] == 0
    for offset, date, expected in (
        (6102, "21", "DEFENSIVE"),
        (6103, "22", "DEFENSIVE"),
        (6104, "25", "CAUTION"),
        (6105, "26", "CAUTION"),
        (6106, "27", "NORMAL"),
    ):
        prior = observe(
            settings,
            command,
            prior,
            f"recovery-{offset}",
            f"2042-05-{date}T16:00:00Z",
            equity="1250",
            quantity=20,
            session=offset,
        )
        state = prior.result.model_dump(mode="json")["drawdown"]["state"]
        assert state["risk_state"] == expected
        assert Decimal(state["maximum_drawdown"]) == Decimal("0.16")
        assert Decimal(state["units"]) == Decimal("1300")


@pytest.mark.parametrize(
    ("cash_delta", "asset_quantity"),
    [("400", 0), ("-40", 0), ("0", 10), ("0", -10)],
)
def test_external_cash_flow_changes_units_without_erasing_drawdown(
    migrated_settings: Settings, cash_delta: str, asset_quantity: int
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    before = observe(
        settings,
        payload["drawdown"],
        opened.report,
        "pre-flow",
        "2042-05-18T16:00:00Z",
        equity="1092",
    )
    snapshot = position_snapshot_command()
    snapshot.update(
        snapshot_id="synthetic-position-after-cash-flow", cutoff_at="2042-05-19T16:00:00Z"
    )
    snapshot["accounts"] = snapshot["accounts"][:1]
    snapshot["annotations"] = []
    refresh_current_position_evidence(snapshot, snapshot["cutoff_at"])
    account = snapshot["accounts"][0]
    delta = Decimal(cash_delta)
    account["account_equity"] = str(Decimal("1102") + delta + Decimal("9.92") * asset_quantity)
    account["positions"][0]["market_price"] = "9.92"
    if asset_quantity:
        account["positions"][0].update(
            total_quantity=str(100 + asset_quantity),
            broker_sellable_quantity=str(70 + asset_quantity),
            reported_cost_basis=str(900 + asset_quantity * 9),
        )
    for field in ("ledger_cash", "trading_cash", "transferable_cash"):
        account["cash_state"][field] = str(Decimal(account["cash_state"][field]) + delta)
    account["ledger_entries"].append(
        {
            "entry_id": "synthetic-external-cash-flow",
            "entry_type": "TRANSFER_IN" if delta > 0 or asset_quantity > 0 else "TRANSFER_OUT",
            "security_id": account["positions"][0]["security_id"] if asset_quantity else None,
            "quantity_delta": str(asset_quantity),
            "cost_basis_delta": str(asset_quantity * 9),
            "cash_delta": cash_delta,
            "occurred_at": "2042-05-19T15:00:00Z",
            "evidence": position_evidence("synthetic-cash-flow", cutoff_at=snapshot["cutoff_at"]),
        }
    )
    position = run_frozen_decision_case(
        settings, position_case_payload(settings, "drawdown-cash-flow", snapshot)
    )
    assert position.report is not None
    assert position.report.result.position is not None
    assert position.report.result.position.disposition == "RECONCILED"
    command = deepcopy(payload["drawdown"])
    before_state = before.result.model_dump(mode="json")["drawdown"]["state"]
    command.update(
        operation="OBSERVE",
        cutoff_at=snapshot["cutoff_at"],
        previous_decision_id=before.event_id,
        capital_flows=[
            {
                "flow_id": "synthetic-flow-alpha",
                "kind": "EXTERNAL",
                "occurred_at": "2042-05-19T15:00:00Z",
                "before_valuation": before_state["valuation"],
                "ledger_keys": [
                    {
                        "account_id": "synthetic-account-4017",
                        "entry_id": "synthetic-external-cash-flow",
                    }
                ],
            }
        ],
    )
    command["valuation"].update(
        position_event_id=position.report.event_id, evidence=snapshot["snapshot_evidence"]
    )
    result = run_frozen_decision_case(settings, drawdown_case(settings, "cash-flow", command))
    assert result.report is not None
    outcome = result.report.result.model_dump(mode="json")["drawdown"]
    assert outcome["disposition"] == "ACCEPTED"
    assert Decimal(outcome["state"]["unit_nav"]) == Decimal("0.84")
    assert Decimal(outcome["state"]["current_drawdown"]) == Decimal("0.16")
    assert outcome["state"]["risk_state"] == "DEFENSIVE"
    assert Decimal(outcome["state"]["units"]) != Decimal("1300")


def test_preservation_closes_only_after_execution_and_cooling_then_requires_new_authorization(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    command = payload["drawdown"]
    preservation = observe(
        settings,
        command,
        opened.report,
        "preservation",
        "2042-05-18T16:00:00Z",
        equity="1027",
    )
    rebound = observe(
        settings,
        command,
        preservation,
        "preservation-rebound",
        "2042-05-19T16:00:00Z",
        equity="1300",
    )
    assert (
        rebound.result.model_dump(mode="json")["drawdown"]["state"]["risk_state"] == "PRESERVATION"
    )
    denied = observe(
        settings,
        command,
        rebound,
        "close-residual",
        "2042-05-20T16:00:00Z",
        equity="1300",
        operation="CLOSE",
    )
    assert denied.result.model_dump(mode="json")["drawdown"]["disposition"] == "DENIED"
    closed = observe(
        settings,
        command,
        denied,
        "close-flat",
        "2042-05-20T17:00:00Z",
        equity="1300",
        operation="CLOSE",
        quantity=0,
        settled=True,
    )
    state = closed.result.model_dump(mode="json")["drawdown"]["state"]
    assert state["epoch_status"] == "CLOSED"
    assert state["cooling_sessions"] == 0
    assert state["risk_state"] == "PRESERVATION"
    prior = closed
    for ordinal, day, count in ((6102, "21", 1), (6103, "22", 2), (6104, "25", 3), (6105, "26", 4)):
        prior = observe(
            settings,
            command,
            prior,
            f"cool-{ordinal}",
            f"2042-05-{day}T16:00:00Z",
            equity="1300",
            quantity=0,
            settled=True,
            session=ordinal,
        )
        state = prior.result.model_dump(mode="json")["drawdown"]["state"]
        assert state["cooling_sessions"] == count
        assert state["new_exposure_blocked"] is True
        assert state["risk_state"] == "PRESERVATION"
    proposal = portfolio_proposal()
    proposal["cash_obligations"] = []
    proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-recapital-selection",
        cutoff="2042-05-27T16:00:00Z",
    )
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-recapital-effective",
        cutoff="2042-05-28T16:00:00Z",
    )
    proposal["risk_budget"].update(
        version_id="synthetic-recapital-budget",
        effective_at="2042-05-28T16:00:00Z",
        expires_at="2042-11-28T16:00:00Z",
    )
    authorization = run_frozen_decision_case(
        settings,
        portfolio_case_payload(
            settings,
            "recapital",
            portfolio_confirmation_command(
                proposal, previous_authorization_id=command["authorization_id"]
            ),
        ),
        clock=GovernanceClock("2042-05-28T16:01:00Z"),
    )
    assert authorization.report is not None
    assert authorization.report.result.portfolio is not None
    capital = authorization.report.result.portfolio.authorization
    assert capital is not None, authorization.report.result.portfolio.reasons
    successor = deepcopy(command)
    successor.update(
        epoch_id="synthetic-capital-epoch-beta",
        authorization_id=capital.authorization_id,
        reauthorization={
            "confirmation_id": "synthetic-capital-confirmation-beta",
            "authorization_id": capital.authorization_id,
            "previous_epoch_id": "synthetic-capital-epoch-alpha",
            "epoch_id": "synthetic-capital-epoch-beta",
            "confirmed_at": "2042-05-27T16:00:00Z",
            "confirmed": True,
        },
    )
    new = observe(
        settings,
        successor,
        prior,
        "recapital-open",
        "2042-05-28T16:00:00Z",
        equity="1300",
        quantity=0,
        settled=True,
        operation="REAUTHORIZE",
    )
    state = new.result.model_dump(mode="json")["drawdown"]["state"]
    assert state["epoch_id"] == "synthetic-capital-epoch-beta"
    assert state["previous_epoch_id"] == "synthetic-capital-epoch-alpha"
    assert state["risk_state"] == "NORMAL"
    assert Decimal(state["current_drawdown"]) == 0
    assert closed.result.model_dump(mode="json")["drawdown"]["state"]["maximum_drawdown"] == "0.21"


def test_budget_expiry_keeps_epoch_and_protection_without_authorizing_new_exposure(
    migrated_settings: Settings,
) -> None:
    payload = opening_payload(migrated_settings)
    opened = run_frozen_decision_case(migrated_settings, payload)
    assert opened.report is not None
    expired = observe(
        migrated_settings,
        payload["drawdown"],
        opened.report,
        "expired-budget",
        "2042-11-17T16:00:00Z",
        equity="1092",
    )
    outcome = expired.result.model_dump(mode="json")["drawdown"]
    assert outcome["state"]["epoch_id"] == "synthetic-capital-epoch-alpha"
    assert outcome["state"]["risk_state"] == "DEFENSIVE"
    assert outcome["state"]["new_exposure_blocked"] is True
    assert Decimal(outcome["state"]["units"]) == Decimal("1300")
    assert Decimal(outcome["state"]["high_water_nav"]) == 1


@pytest.mark.parametrize(
    ("sessions", "hard_gate", "expected_count"),
    [
        ((6101, 6103), False, 1),
        ((6101, 6102), True, 0),
    ],
)
def test_missing_market_session_or_new_hard_gate_resets_recovery(
    migrated_settings: Settings,
    sessions: tuple[int, int],
    hard_gate: bool,
    expected_count: int,
) -> None:
    payload = opening_payload(migrated_settings)
    opened = run_frozen_decision_case(migrated_settings, payload)
    assert opened.report is not None
    command = payload["drawdown"]
    prior = observe(
        migrated_settings,
        command,
        opened.report,
        "guard-caution",
        "2042-05-18T16:00:00Z",
        equity="1157",
    )
    prior = observe(
        migrated_settings,
        command,
        prior,
        "guard-first",
        "2042-05-20T16:00:00Z",
        equity="1250",
        session=sessions[0],
    )
    date = "21" if sessions[1] == 6102 else "22"
    prior = observe(
        migrated_settings,
        command,
        prior,
        "guard-second",
        f"2042-05-{date}T16:00:00Z",
        equity="1250",
        session=sessions[1],
        hard_gate=hard_gate,
    )
    state = prior.result.model_dump(mode="json")["drawdown"]["state"]
    assert state["risk_state"] == "CAUTION"
    assert state["recovery_sessions"] == expected_count


def test_exhausted_net_liquidation_equity_escalates_instead_of_dropping_protection(
    migrated_settings: Settings,
) -> None:
    payload = opening_payload(migrated_settings)
    opened = run_frozen_decision_case(migrated_settings, payload)
    assert opened.report is not None
    position = position_snapshot_command()
    position["accounts"] = position["accounts"][:1]
    position["annotations"] = []
    position.update(snapshot_id="synthetic-exhausted", cutoff_at="2042-05-18T16:00:00Z")
    refresh_current_position_evidence(position, position["cutoff_at"])
    result = run_frozen_decision_case(
        migrated_settings, position_case_payload(migrated_settings, "exhausted", position)
    )
    assert result.report is not None
    command = payload["drawdown"]
    command.update(
        operation="OBSERVE",
        previous_decision_id=opened.report.event_id,
        cutoff_at=position["cutoff_at"],
    )
    command["valuation"].update(
        position_event_id=result.report.event_id,
        liquidation_cost="1310",
        evidence=position["snapshot_evidence"],
    )
    observed = run_frozen_decision_case(
        migrated_settings, drawdown_case(migrated_settings, "exhausted", command)
    )
    assert observed.report is not None
    outcome = observed.report.result.model_dump(mode="json")["drawdown"]
    assert outcome["state"]["risk_state"] == "PRESERVATION"
    assert outcome["state"]["stock_exposure_limit"] == "0"
    assert outcome["state"]["new_exposure_blocked"] is True


def test_versioned_long_calendar_supports_full_sequential_recovery(
    migrated_settings: Settings,
) -> None:
    payload = opening_payload(migrated_settings)
    command = payload["drawdown"]
    command["policy"].update(
        market_calendar_version_id="synthetic-capital-calendar-v1",
        defensive_recovery_sessions=23,
        caution_recovery_sessions=5,
    )
    opened = run_frozen_decision_case(migrated_settings, payload)
    assert opened.report is not None
    outcome = opened.report.result.model_dump(mode="json")["drawdown"]
    assert outcome["disposition"] == "ACCEPTED", outcome["reasons"]
    prior = observe(
        migrated_settings,
        command,
        opened.report,
        "long-defensive",
        "2042-05-18T16:00:00Z",
        equity="1092",
    )
    offsets = [0, 1, 2] + [5 + week * 7 + day for week in range(5) for day in range(5)]
    for index, offset in enumerate(offsets):
        cutoff = datetime.fromisoformat("2042-05-20T16:00:00+00:00") + timedelta(days=offset)
        prior = observe(
            migrated_settings,
            command,
            prior,
            f"long-recovery-{index}",
            cutoff.isoformat(),
            equity="1250",
            quantity=20,
            session=7101 + index,
        )
        state = prior.result.model_dump(mode="json")["drawdown"]["state"]
        expected = "DEFENSIVE" if index < 22 else "CAUTION" if index < 27 else "NORMAL"
        assert state["risk_state"] == expected
        assert Decimal(state["maximum_drawdown"]) == Decimal("0.16")


def test_cli_and_authenticated_http_return_the_identical_saved_capital_record(
    migrated_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = migrated_settings.model_copy(
        update={"report_account_ids": ("synthetic-account-4017",)}
    )
    payload = opening_payload(settings)
    case_path = tmp_path / "synthetic-capital-case.json"
    case_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stock-profiler",
            "decision-case-run",
            "--case",
            str(case_path),
        ],
    )
    cli.main()
    execution = json.loads(capsys.readouterr().out)
    assert execution["publication_status"] == "PUBLISHED"
    replay = run_frozen_decision_case(settings, payload)
    assert replay.report is not None
    with TestClient(create_app(settings), base_url="https://localhost") as client:
        path = f"/api/v1/reports/{execution['report_version_id']}"
        assert client.get(path).status_code == 401
        token, _ = PasskeyAuthenticator(
            initialize_runtime_storage(settings).engine,
            settings,
        )._create_session("synthetic-capital-cli-session")
        client.cookies.set(settings.auth_session_cookie_name, token)
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == replay.report.model_dump(mode="json")
        assert response.json()["result"]["drawdown"]["state"]["unit_nav"] == "1"


def test_budget_renewal_preserves_peak_units_and_epoch(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    peak = observe(
        settings,
        payload["drawdown"],
        opened.report,
        "renewal-peak",
        "2042-05-18T16:00:00Z",
        equity="1560",
    )
    proposal = portfolio_proposal()
    proposal["cash_obligations"] = []
    proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-renewal-selection",
        cutoff="2042-05-19T16:00:00Z",
    )
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-renewal-effective",
        cutoff="2042-05-20T16:00:00Z",
    )
    proposal["risk_budget"].update(
        version_id="synthetic-renewal-budget",
        effective_at="2042-05-20T16:00:00Z",
        expires_at="2042-11-20T16:00:00Z",
    )
    renewal = run_frozen_decision_case(
        settings,
        portfolio_case_payload(
            settings,
            "capital-renewal",
            portfolio_confirmation_command(
                proposal,
                previous_authorization_id=payload["drawdown"]["authorization_id"],
            ),
        ),
        clock=GovernanceClock("2042-05-20T16:01:00Z"),
    )
    assert renewal.report is not None and renewal.report.result.portfolio is not None
    capital = renewal.report.result.portfolio.authorization
    assert capital is not None, renewal.report.result.portfolio.reasons
    command = deepcopy(payload["drawdown"])
    command["authorization_id"] = capital.authorization_id
    after = observe(
        settings,
        command,
        peak,
        "renewed-capital",
        "2042-05-20T16:00:00Z",
        equity="1300",
    )
    state = after.result.model_dump(mode="json")["drawdown"]["state"]
    assert state["epoch_id"] == "synthetic-capital-epoch-alpha"
    assert Decimal(state["high_water_nav"]) == Decimal("1.2")
    assert Decimal(state["units"]) == 1300
    assert state["risk_state"] == "DEFENSIVE"
