from __future__ import annotations

from copy import deepcopy

import pytest
from test_drawdown_closure import cooled_epoch
from test_drawdown_integrity import cash_snapshot, transfer
from test_drawdown_protection import drawdown_case, observe, opening_payload

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings


@pytest.mark.parametrize("expiry_source", ["valuation", "equity"])
def test_expired_pre_flow_evidence_cannot_price_capital_units(
    migrated_settings: Settings,
    expiry_source: str,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    prior = observe(
        settings,
        payload["drawdown"],
        opened.report,
        "expiry-defensive",
        "2042-05-18T16:00:00Z",
        equity="1092",
    )
    expiry = "2042-05-18T18:00:00Z"
    pre_event, pre_evidence = cash_snapshot(
        settings,
        "expiring-pre-flow",
        "2042-05-18T17:00:00Z",
        equity="1092",
        transfers=[],
        equity_expiry=expiry if expiry_source == "equity" else None,
    )
    if expiry_source == "valuation":
        pre_evidence["expires_at"] = expiry
    cutoff = "2042-05-19T16:00:00Z"
    flow = transfer("expired-price-flow", "2042-05-19T15:00:00Z", cutoff)
    post_event, post_evidence = cash_snapshot(
        settings,
        "expired-price-post-flow",
        cutoff,
        equity="1492",
        transfers=[flow],
    )
    command = deepcopy(payload["drawdown"])
    command.update(
        operation="OBSERVE",
        previous_decision_id=prior.event_id,
        cutoff_at=cutoff,
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
    result = run_frozen_decision_case(settings, drawdown_case(settings, "expired-price", command))
    assert result.report is not None and result.report.result.drawdown is not None
    outcome = result.report.result.drawdown
    assert outcome.disposition == "UNKNOWN"
    assert outcome.state is not None and outcome.state.units == 1300
    assert outcome.state.risk_state == "DEFENSIVE"


@pytest.mark.parametrize("extra_session", [None, 6101])
def test_benign_or_duplicate_observation_preserves_existing_close_progress(
    migrated_settings: Settings,
    extra_session: int | None,
) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    command = payload["drawdown"]
    prior = observe(
        settings,
        command,
        opened.report,
        "frequency-caution",
        "2042-05-18T16:00:00Z",
        equity="1157",
    )
    prior = observe(
        settings,
        command,
        prior,
        "frequency-close",
        "2042-05-20T16:00:00Z",
        equity="1250",
        session=6101,
    )
    extra = observe(
        settings,
        command,
        prior,
        "frequency-extra",
        "2042-05-20T17:00:00Z",
        equity="1250",
        session=extra_session,
    )
    assert extra.result.drawdown is not None and extra.result.drawdown.state is not None
    assert extra.result.drawdown.state.recovery_sessions == 1
    recovered = observe(
        settings,
        command,
        extra,
        "frequency-next-close",
        "2042-05-21T16:00:00Z",
        equity="1250",
        session=6102,
    )
    assert recovered.result.drawdown is not None and recovered.result.drawdown.state is not None
    assert recovered.result.drawdown.state.risk_state == "NORMAL"


def test_same_close_cannot_be_reused_across_recovery_levels(migrated_settings: Settings) -> None:
    settings = migrated_settings
    payload = opening_payload(settings)
    command = payload["drawdown"]
    command["policy"].update(defensive_recovery_sessions=1, caution_recovery_sessions=1)
    opened = run_frozen_decision_case(settings, payload)
    assert opened.report is not None
    prior = observe(
        settings,
        command,
        opened.report,
        "single-close-defensive",
        "2042-05-18T16:00:00Z",
        equity="1092",
    )
    prior = observe(
        settings,
        command,
        prior,
        "single-close-caution",
        "2042-05-20T16:00:00Z",
        equity="1250",
        quantity=20,
        session=6101,
    )
    repeated = observe(
        settings,
        command,
        prior,
        "single-close-repeat",
        "2042-05-20T17:00:00Z",
        equity="1250",
        quantity=20,
        session=6101,
    )
    assert repeated.result.drawdown is not None and repeated.result.drawdown.state is not None
    assert repeated.result.drawdown.state.risk_state == "CAUTION"
    assert repeated.result.drawdown.state.recovery_sessions == 0


def test_benign_observation_preserves_completed_cooling(migrated_settings: Settings) -> None:
    command, cooled = cooled_epoch(migrated_settings)
    extra = observe(
        migrated_settings,
        command,
        cooled,
        "benign-cooled-observation",
        "2042-05-26T17:00:00Z",
        equity="1300",
        quantity=0,
        settled=True,
    )
    assert extra.result.drawdown is not None and extra.result.drawdown.state is not None
    assert extra.result.drawdown.state.cooling_sessions == 4
