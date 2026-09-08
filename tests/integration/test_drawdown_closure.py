from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

from test_drawdown_protection import observe, opening_payload
from test_portfolio_authorization import (
    portfolio_case_payload,
    portfolio_confirmation_command,
    portfolio_proposal,
    snapshot_at,
)
from test_position_state_reconciliation import position_evidence
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import FormalReport


def closed_epoch(settings: Settings) -> tuple[dict[str, Any], FormalReport]:
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    command = payload["drawdown"]
    prior = observe(
        settings,
        command,
        opened.report,
        "close-preservation",
        "2042-05-18T16:00:00Z",
        equity="1027",
    )
    closed = observe(
        settings,
        command,
        prior,
        "closed-epoch",
        "2042-05-20T17:00:00Z",
        equity="1300",
        quantity=0,
        settled=True,
        operation="CLOSE",
    )
    assert closed.result.drawdown is not None and closed.result.drawdown.state is not None
    assert closed.result.drawdown.state.epoch_status == "CLOSED"
    return command, closed


def cooled_epoch(settings: Settings) -> tuple[dict[str, Any], FormalReport]:
    command, prior = closed_epoch(settings)
    for ordinal, day in ((6102, "21"), (6103, "22"), (6104, "25"), (6105, "26")):
        prior = observe(
            settings,
            command,
            prior,
            f"closed-cooling-{ordinal}",
            f"2042-05-{day}T16:00:00Z",
            equity="1300",
            quantity=0,
            settled=True,
            session=ordinal,
        )
    assert prior.result.drawdown is not None and prior.result.drawdown.state is not None
    assert prior.result.drawdown.state.cooling_sessions == 4
    return command, prior


def roundtrip() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "entry_id": f"synthetic-cooling-roundtrip-{index}",
            "entry_type": "FILL",
            "security_id": "XQZ-4017",
            "quantity_delta": str(quantity),
            "cost_basis_delta": str(quantity * 12),
            "cash_delta": str(-quantity * 12),
            "occurred_at": date,
            "evidence": position_evidence(
                f"synthetic-cooling-roundtrip-{index}",
                cutoff_at="2042-06-04T16:00:00Z",
            ),
        }
        for index, (quantity, date) in enumerate(
            (
                (10, "2042-05-28T15:00:00Z"),
                (-10, "2042-06-01T15:00:00Z"),
            )
        )
    )


def test_rejected_closure_still_commits_qualified_preservation_breach(
    migrated_settings: Settings,
) -> None:
    payload = opening_payload(migrated_settings)
    opened = run_frozen_decision_case(migrated_settings, payload)
    assert opened.report is not None
    rejected = observe(
        migrated_settings,
        payload["drawdown"],
        opened.report,
        "rejected-breach",
        "2042-05-18T16:00:00Z",
        equity="1000",
        operation="CLOSE",
    )
    outcome = rejected.result.drawdown
    assert outcome is not None and outcome.disposition == "DENIED"
    assert outcome.state is not None
    assert outcome.state.risk_state == "PRESERVATION"
    assert outcome.state.epoch_status == "OPEN"
    rebound = observe(
        migrated_settings,
        payload["drawdown"],
        rejected,
        "after-rejected-breach",
        "2042-05-19T16:00:00Z",
        equity="1300",
    )
    assert rebound.result.drawdown is not None and rebound.result.drawdown.state is not None
    assert rebound.result.drawdown.state.risk_state == "PRESERVATION"
    assert rebound.result.drawdown.state.maximum_drawdown > Decimal("0.21")


def test_preclosure_session_does_not_count_toward_cooling(migrated_settings: Settings) -> None:
    command, closed = closed_epoch(migrated_settings)
    later = observe(
        migrated_settings,
        command,
        closed,
        "same-day-cooling",
        "2042-05-20T18:00:00Z",
        equity="1300",
        quantity=0,
        settled=True,
        session=6101,
    )
    assert later.result.drawdown is not None and later.result.drawdown.state is not None
    assert later.result.drawdown.state.cooling_sessions == 0


def test_intervening_roundtrip_restarts_closed_epoch_cooling(migrated_settings: Settings) -> None:
    command, cooled = cooled_epoch(migrated_settings)
    later = observe(
        migrated_settings,
        command,
        cooled,
        "intervening-cooling-trades",
        "2042-06-04T16:00:00Z",
        equity="1300",
        quantity=0,
        settled=True,
        session=6112,
        extra_ledger_entries=roundtrip(),
    )
    assert later.result.drawdown is not None and later.result.drawdown.state is not None
    assert later.result.drawdown.state.cooling_sessions == 1
    assert later.result.drawdown.state.new_exposure_blocked


def test_same_session_roundtrip_invalidates_a_previously_complete_cooling_count(
    migrated_settings: Settings,
) -> None:
    command, cooled = cooled_epoch(migrated_settings)
    entries = deepcopy(roundtrip())
    for index, entry in enumerate(entries):
        entry["occurred_at"] = f"2042-05-27T{10 + index}:00:00Z"
        entry["evidence"] = position_evidence(
            f"synthetic-intraday-cooling-{index}",
            cutoff_at="2042-05-27T16:00:00Z",
        )
    later = observe(
        migrated_settings,
        command,
        cooled,
        "intraday-cooling-trades",
        "2042-05-27T16:00:00Z",
        equity="1300",
        quantity=0,
        settled=True,
        session=6106,
        extra_ledger_entries=entries,
    )
    assert later.result.drawdown is not None and later.result.drawdown.state is not None
    assert later.result.drawdown.state.cooling_sessions == 0


def test_intervening_roundtrip_prevents_reusing_completed_cooling_for_reauthorization(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    command, cooled = cooled_epoch(settings)
    proposal = portfolio_proposal()
    proposal["cash_obligations"] = []
    proposal["snapshot"]["accounts"] = proposal["snapshot"]["accounts"][:1]
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-reopen-selection",
        cutoff="2042-06-03T16:00:00Z",
    )
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-reopen-effective",
        cutoff="2042-06-04T16:00:00Z",
    )
    proposal["risk_budget"].update(
        version_id="synthetic-reopen-budget",
        effective_at="2042-06-04T16:00:00Z",
        expires_at="2042-12-04T16:00:00Z",
    )
    authorization = run_frozen_decision_case(
        settings,
        portfolio_case_payload(
            settings,
            "intervening-trades-reauthorize",
            portfolio_confirmation_command(
                proposal,
                previous_authorization_id=command["authorization_id"],
            ),
        ),
        clock=GovernanceClock("2042-06-04T16:01:00Z"),
    )
    assert authorization.report is not None and authorization.report.result.portfolio is not None
    capital = authorization.report.result.portfolio.authorization
    assert capital is not None, authorization.report.result.portfolio.reasons
    successor = deepcopy(command)
    successor.update(
        epoch_id="synthetic-capital-epoch-beta",
        authorization_id=capital.authorization_id,
        reauthorization={
            "confirmation_id": "synthetic-reopen-confirmation",
            "authorization_id": capital.authorization_id,
            "previous_epoch_id": command["epoch_id"],
            "epoch_id": "synthetic-capital-epoch-beta",
            "confirmed_at": "2042-06-03T16:00:00Z",
            "confirmed": True,
        },
    )
    denied = observe(
        settings,
        successor,
        cooled,
        "reopen-after-intervening-trades",
        "2042-06-04T16:00:00Z",
        equity="1300",
        quantity=0,
        settled=True,
        operation="REAUTHORIZE",
        extra_ledger_entries=roundtrip(),
    )
    assert denied.result.drawdown is not None
    assert denied.result.drawdown.disposition == "DENIED"
    assert denied.result.drawdown.state is not None
    assert denied.result.drawdown.state.epoch_id == command["epoch_id"]
    assert denied.result.drawdown.state.cooling_sessions == 0


def test_full_redemption_retains_closed_epoch_and_can_continue_zero_stock_cooling(
    migrated_settings: Settings,
) -> None:
    settings = migrated_settings
    command, closed = closed_epoch(settings)
    assert closed.result.drawdown is not None and closed.result.drawdown.state is not None
    previous = closed.result.drawdown.state
    withdrawal = {
        "entry_id": "synthetic-full-capital-redemption",
        "entry_type": "TRANSFER_OUT",
        "security_id": None,
        "quantity_delta": "0",
        "cost_basis_delta": "0",
        "cash_delta": "-1300",
        "occurred_at": "2042-05-21T15:00:00Z",
        "evidence": position_evidence(
            "synthetic-full-capital-redemption",
            cutoff_at="2042-05-21T16:00:00Z",
        ),
    }
    command["capital_flows"] = [
        {
            "flow_id": withdrawal["entry_id"],
            "kind": "EXTERNAL",
            "occurred_at": withdrawal["occurred_at"],
            "before_valuation": previous.valuation.model_dump(mode="json"),
            "ledger_keys": [
                {
                    "account_id": "synthetic-account-4017",
                    "entry_id": withdrawal["entry_id"],
                }
            ],
        }
    ]
    redeemed = observe(
        settings,
        command,
        closed,
        "full-capital-redemption",
        "2042-05-21T16:00:00Z",
        equity="0",
        quantity=0,
        settled=True,
        session=6102,
        extra_ledger_entries=(withdrawal,),
    )
    assert redeemed.result.drawdown is not None and redeemed.result.drawdown.state is not None
    state = redeemed.result.drawdown.state
    assert state.units == 0
    assert state.unit_nav is None
    assert state.current_drawdown is None
    assert state.high_water_nav == previous.high_water_nav
    assert state.maximum_drawdown == previous.maximum_drawdown
    assert state.risk_state == "PRESERVATION" and state.epoch_status == "CLOSED"
    assert state.cooling_sessions == 1 and state.new_exposure_blocked
    command["capital_flows"] = []
    later = observe(
        settings,
        command,
        redeemed,
        "after-full-redemption",
        "2042-05-22T16:00:00Z",
        equity="0",
        quantity=0,
        settled=True,
        session=6103,
        extra_ledger_entries=(withdrawal,),
    )
    assert later.result.drawdown is not None and later.result.drawdown.state is not None
    assert later.result.drawdown.state.units == 0
    assert later.result.drawdown.state.cooling_sessions == 2
