from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any

import pytest
from test_execution_plans import execution_routes, risk_handoff_payload
from test_position_state_reconciliation import position_evidence
from test_scoped_qualification import GovernanceClock, case_payload, qualification_command, version
from test_security_exit_assessment import exit_case, qualified_assessment

from stock_profiler.bootstrap.decision_cases import (
    replay_default_frozen_decision_case,
    run_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.position_management.action_contracts import PositionActionCommand


def action_payload(
    settings: Settings,
    *,
    normal: bool = True,
    cutoff_at: str = "2042-05-18T16:00:00Z",
    previous: dict[str, Any] | None = None,
    snapshot_transform: Callable[[dict[str, Any]], None] | None = None,
    gate: str | None = None,
    missing_downside: bool = False,
    capital_state: str | None = None,
    standard_price: str = "10",
    points: tuple[str, str, str, str, str] = ("0.20", "0.15", "0.10", "0.05", "0.75"),
) -> dict[str, Any]:
    cutoff = cutoff_at
    previous_capital = None
    if previous is not None:
        capital_report = replay_default_frozen_decision_case(
            settings, "synthetic:drawdown:plan-capital"
        ).report
        assert capital_report is not None and capital_report.result.drawdown is not None
        assert capital_report.result.drawdown.state is not None
        previous_capital = capital_report.result.drawdown.state.decision_id
    payload = risk_handoff_payload(
        settings,
        normal=normal,
        cutoff_at="2042-05-17T16:00:00Z" if capital_state else cutoff,
        identity_suffix=f":{cutoff}" if previous is not None else "",
        snapshot_transform=snapshot_transform,
        account_cash=("7000", "1000"),
        downside_grid=("0.05", "0.10", "0.23"),
        previous_capital=previous_capital,
        capital_state=capital_state,
    )
    execution = payload.pop("execution_plan")
    # Build a thesis from the same committed authoritative snapshot; no alternate broker history.
    thesis = {
        "operation": "REGISTER_THESIS",
        "contract_version": "1.0.0",
        "cutoff_at": cutoff,
        "position_event_id": execution["concentration_event_id"],
        "position_id": "synthetic-position-4017-xqz",
        "thesis": {
            "thesis_id": "synthetic-action-thesis",
            "version": "synthetic-thesis-v1",
            "security_id": "XQZ-4017",
            "lifecycle_id": "synthetic-lifecycle-4017-xqz",
            "origin": "EXTERNAL",
            "registered_at": cutoff,
            "valid_until": "2042-11-18T16:00:00Z",
            "propositions": [
                {
                    "proposition_id": "synthetic-business-premise",
                    "statement": "Fictional licensed operation remains active.",
                    "metric": "licensed_operation_count",
                    "authority": "DISCLOSURE",
                    "source": "synthetic-original-disclosure",
                    "operator": "LE",
                    "threshold": "0",
                    "trigger_direction": "LIQUIDATE",
                }
            ],
        },
    }
    if previous is None:
        assessment = qualified_assessment(
            settings, thesis_registration=thesis, standard_price=standard_price
        )
    else:
        assessment = {
            "operation": "ASSESS_SECURITY",
            "contract_version": "1.0.0",
            "cutoff_at": cutoff,
            "security_id": "XQZ-4017",
            "lifecycle_id": thesis["thesis"]["lifecycle_id"],
            "thesis_event_id": previous["thesis_event_id"],
            "market_state": "synthetic-steady",
            "board": "synthetic-board",
            "standard_price": standard_price,
            "calendar_version": "synthetic-session-calendar-v1",
            "evidence": deepcopy(previous["evidence"]),
            "predictions": [],
        }
        for evidence in assessment["evidence"]:
            evidence["valid_until"] = "2042-11-18T16:00:00Z"
        assessment = qualified_assessment(
            settings, frozen_assessment=assessment, standard_price=standard_price
        )

    for prediction, point in zip(assessment["predictions"], points, strict=True):
        prediction["point"] = point
        prediction["valid_until"] = (
            (datetime.fromisoformat(cutoff) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        )
    if gate == "legal":
        assessment["evidence"][0]["legal_termination"] = True
    elif gate == "falsified":
        assessment["evidence"][2]["value"] = "0"
    if gate:
        assessment["predictions"] = []
    elif missing_downside:
        assessment["predictions"] = [
            p for p in assessment["predictions"] if p["qualification_scope"]["target"] == "V60"
        ]
    security = run_frozen_decision_case(
        settings,
        exit_case(settings, f"action-security:{cutoff}", assessment),
        clock=GovernanceClock(assessment["cutoff_at"]),
    )
    assert security.report is not None
    routes = execution_routes()
    for route in routes:
        route["first_sellable_at"] = cutoff
        route["transferable_at"] = (
            (datetime.fromisoformat(cutoff) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        )
        route["rules_evidence"] = position_evidence(
            "synthetic-rules", cutoff_at=assessment["cutoff_at"]
        )
        route["cost_curve"]["evidence"] = position_evidence(
            "synthetic-cost", cutoff_at=assessment["cutoff_at"]
        )
    execution["routes"] = routes
    policy_version = version(settings, "synthetic-action-policy-alpha")
    policy = {
        "synthetic": True,
        "generator_version": "position-action-v1",
        "seed": 29913,
        "version": policy_version,
        "normal_grid": "0.10",
        "risk_grid": "0.05",
        "downside_safe_upper": "0.20",
        "downside_risky_lower": "0.40",
        "value_established_lower": "0.70",
        "value_denied_upper": "0.30",
        "reduction_ratio": "0.50",
        "lifecycle_cost_ratio": "0.01",
        "maximum_cost_ratio": "0.02",
        "maximum_minimum_commission": "10",
        "maximum_dividend_tax_per_share": "0.2",
    }
    grant = qualification_command(settings)
    grant["version"] = policy_version
    grant["scope"].update(
        capability="position-action",
        purpose="initial-position-action",
        target="D20/V60",
        source="EXTERNAL",
        market_state=assessment["market_state"],
        board=assessment["board"],
        account_ids=payload["access_scope"]["account_ids"],
        account_type="SIMULATED_CASH",
        holding_age_domain="INITIAL",
        probability_grid="0.10" if normal else "0.05",
        portfolio_scope=execution["portfolio_id"],
    )
    grant["evidence"].update(
        version=policy_version,
        scope=grant["scope"],
        digest=sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        market_calendar_version=assessment["calendar_version"],
    )
    qualification_payload = case_payload(settings, "action-policy-grant", grant)
    qualification_payload["access_scope"] = deepcopy(payload["access_scope"])
    saved = run_frozen_decision_case(settings, qualification_payload, clock=GovernanceClock())
    assert saved.report is not None and saved.report.result.governance is not None
    assert saved.report.result.governance.disposition == "APPROVED"
    command = {
        "contract_version": "1.0.0",
        "operation": "POSITION_ACTION",
        "cutoff_at": assessment["cutoff_at"],
        "security_id": assessment["security_id"],
        "lifecycle_id": assessment["lifecycle_id"],
        "security_event_id": security.decision_event_id,
        "execution": execution,
        "policy": policy,
        "qualification_scope": grant["scope"],
        "qualification_id": saved.decision_event_id,
    }
    payload["version_bundle"].update(
        case_contract_version="position-action.1.0.0",
        host_contract_version="position-action.1.0.0",
        report_projection_contract_version="position-action.1.0.0",
    )
    command = PositionActionCommand.model_validate(command).model_dump(mode="json")
    payload["position_action"] = payload["input"]["position_action"] = command
    if previous is not None:
        payload["case_id"] += cutoff
        payload["business_identity"] += cutoff
    return payload


def test_safe_downside_and_established_value_hold_verified_quantity_without_buy_authority(
    migrated_settings: Settings,
) -> None:
    payload = action_payload(migrated_settings)
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None
    action = saved.report.result.position_action
    assert action is not None
    assert action.direction == "HOLD", (action.selected_grid, action.reasons)
    assert action.target_quantity == Decimal("200")
    assert action.selected_grid == "0.10"
    assert not action.buy_authorized


def test_value_denial_can_exit_without_a_qualified_downside_guard(
    migrated_settings: Settings,
) -> None:
    payload = action_payload(
        migrated_settings,
        missing_downside=True,
        points=("0.20", "0.15", "0.10", "0.05", "0.25"),
    )
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None and saved.report.result.position_action is not None
    assert saved.report.result.position_action.direction == "EXIT"
    assert saved.report.result.position_action.target_quantity == 0


def test_missing_selected_model_evidence_is_a_data_observation_not_matrix_abstention(
    migrated_settings: Settings,
) -> None:
    payload = action_payload(migrated_settings, missing_downside=True)
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None and saved.report.result.position_action is not None
    action = saved.report.result.position_action
    assert action.direction is None and action.disposition == "DATA_OBSERVATION"
    assert "STATISTICAL_EVIDENCE_UNAVAILABLE" in action.reasons


@pytest.mark.parametrize("gate", [None, "legal"])
def test_missing_security_or_position_handoff_preserves_independent_hard_target(
    migrated_settings: Settings, gate: str | None
) -> None:
    payload = action_payload(migrated_settings, normal=False, gate=gate)
    command = payload["position_action"]
    if gate:
        command["execution"]["concentration_event_id"] = "synthetic-missing-position"
    else:
        command["security_event_id"] = "synthetic-missing-security"
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None and saved.report.result.position_action is not None
    action = saved.report.result.position_action
    assert action.direction == ("EXIT" if gate else "REDUCE")
    assert action.target_quantity == (0 if gate else 130)
    assert action.execution is not None and action.execution.disposition == "BLOCKED"
    if gate:
        assert action.current_quantity is None
        assert action.cost_anchor is None
    else:
        assert action.cost_anchor is not None and action.cost_anchor.market_value == 2000


@pytest.mark.parametrize("kind", ["SUSPENSION", "LIMIT_DOWN", "NO_BIDS", "SELLABLE_SHORTFALL"])
def test_legal_execution_block_keeps_statistical_direction_and_unfilled_gap(
    migrated_settings: Settings, kind: str
) -> None:
    def restrict(snapshot: dict[str, Any]) -> None:
        for account in snapshot["accounts"]:
            if kind == "SELLABLE_SHORTFALL":
                account["positions"][0]["broker_sellable_quantity"] = "20"
                account["positions"][0]["frozen_quantity"] = "80"
            else:
                account["execution_restrictions"] = [
                    {
                        "restriction_id": f"synthetic-{kind}",
                        "security_id": "XQZ-4017",
                        "kind": kind,
                        "reason": "Synthetic unavailable execution",
                        "active": True,
                        "evidence": position_evidence(
                            "synthetic-block", cutoff_at=snapshot["cutoff_at"]
                        ),
                    }
                ]

    payload = action_payload(
        migrated_settings,
        snapshot_transform=restrict,
        points=("0.55", "0.50", "0.30", "0.20", "0.75"),
    )
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None and saved.report.result.position_action is not None
    action = saved.report.result.position_action
    assert action.direction == "REDUCE" and action.target_quantity == 100
    assert action.execution is not None and action.execution.disposition == "BLOCKED"
    assert action.execution.targets[0].remaining_gap is not None
    assert action.execution.targets[0].remaining_gap > 0
    assert run_frozen_decision_case(migrated_settings, payload).report == saved.report


@pytest.mark.parametrize("gate", ["legal", "falsified"])
def test_authoritative_liquidation_precedes_missing_models_and_costs(
    migrated_settings: Settings, gate: str
) -> None:
    payload = action_payload(migrated_settings, gate=gate)
    payload["position_action"]["execution"]["routes"] = []
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None
    action = saved.report.result.position_action
    assert action is not None and action.direction == "EXIT"
    assert action.target_quantity == 0
    assert action.execution is not None and action.execution.disposition == "BLOCKED"
    assert "EXECUTION_COST_EVIDENCE_FAILED" in action.execution.reasons
    assert action.execution.targets[0].required_sale_quantity == 200


@pytest.mark.parametrize(
    "down,value,direction,target",
    [
        (("0.20", "0.16", "0.10", "0.05"), "0.73", "HOLD", "200"),
        (("0.20", "0.16", "0.10", "0.05"), "0.50", None, None),
        (("0.20", "0.16", "0.10", "0.05"), "0.26", "EXIT", "0"),
        (("0.55", "0.43", "0.30", "0.20"), "0.73", "REDUCE", "100"),
        (("0.55", "0.43", "0.30", "0.20"), "0.50", "REDUCE", "100"),
        (("0.55", "0.43", "0.30", "0.20"), "0.26", "EXIT", "0"),
        (("0.35", "0.30", "0.20", "0.10"), "0.73", None, None),
        (("0.35", "0.30", "0.20", "0.10"), "0.50", None, None),
        (("0.35", "0.30", "0.20", "0.10"), "0.26", "EXIT", "0"),
    ],
)
def test_guarded_two_target_matrix_has_no_default_hold(
    migrated_settings: Settings,
    down: tuple[str, str, str, str],
    value: str,
    direction: str | None,
    target: str | None,
) -> None:
    payload = action_payload(migrated_settings, points=(*down, value))
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None
    action = saved.report.result.position_action
    assert action is not None
    assert action.direction == direction
    assert action.target_quantity == (Decimal(target) if target else None)
    assert action.disposition == ("CONCLUDED" if direction else "ABSTAINED")
    if direction == "REDUCE":
        assert action.anchor is not None and action.anchor.quantity == 200
        assert action.execution is not None and action.execution.targets[0].target_quantity == 100


@pytest.mark.parametrize(
    "points", [("0.20", "0.15", "0.10", "0.05", "0.75"), ("0.35", "0.30", "0.20", "0.10", "0.50")]
)
def test_risk_hard_cap_overrides_statistical_hold_or_abstention_and_selects_strict_grid(
    migrated_settings: Settings, points: tuple[str, str, str, str, str]
) -> None:
    payload = action_payload(migrated_settings, normal=False, points=points)
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None
    action = saved.report.result.position_action
    assert action is not None and action.direction == "REDUCE"
    assert action.selected_grid == "0.05"
    assert action.target_quantity == 130
    assert action.execution is not None and action.execution.targets[0].target_quantity == 130


def test_missing_required_grid_never_falls_back_to_available_looser_grid(
    migrated_settings: Settings,
) -> None:
    payload = action_payload(migrated_settings, normal=False)
    payload["position_action"]["qualification_scope"]["probability_grid"] = "0.10"
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None
    action = saved.report.result.position_action
    assert action is not None and action.direction == "REDUCE"
    assert action.model_direction is None
    assert "ACTION_QUALIFICATION_UNAVAILABLE" in action.reasons
    assert action.target_quantity == 130


def test_preservation_liquidation_has_no_statistical_participation(
    migrated_settings: Settings,
) -> None:
    payload = action_payload(
        migrated_settings,
        normal=False,
        capital_state="PRESERVATION",
        points=("0.55", "0.50", "0.30", "0.20", "0.75"),
    )
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None and saved.report.result.position_action is not None
    action = saved.report.result.position_action
    assert action.direction == "EXIT" and action.target_quantity == 0
    assert action.model_direction is None and action.anchor is None


@pytest.mark.parametrize("normal", [True, False])
@pytest.mark.parametrize("defect", ["missing", "envelope", "lifecycle"])
def test_cost_failures_abstain_statistics_but_keep_independent_risk_targets(
    migrated_settings: Settings, normal: bool, defect: str
) -> None:
    payload = action_payload(
        migrated_settings, normal=normal, points=("0.55", "0.50", "0.30", "0.20", "0.75")
    )
    routes = payload["position_action"]["execution"]["routes"]
    if defect == "missing":
        routes[0]["cost_curve"] = None
    elif defect == "envelope":
        routes[0]["cost_curve"]["slippage_ratio"] = "0.03"
    else:
        for route in routes:
            route["cost_curve"]["minimum_commission"] = "30"
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None
    action = saved.report.result.position_action
    assert action is not None
    assert action.model_direction == "REDUCE"
    assert action.direction == (None if normal else "REDUCE")
    assert action.target_quantity == (None if normal else Decimal(130))
    assert action.disposition == (
        "DATA_OBSERVATION"
        if normal and defect == "missing"
        else "ABSTAINED"
        if normal
        else "CONCLUDED"
    )
    assert action.anchor is None
    assert (
        "EXECUTION_COST_EVIDENCE_FAILED" in action.reasons
        if defect == "missing"
        else "STATISTICAL_COST_OUT_OF_SCOPE" in action.reasons
    )


@pytest.mark.parametrize("missing_handoff", [None, "security", "position"])
def test_repeated_reduction_freezes_first_anchor_and_does_not_halve_remaining_exposure(
    migrated_settings: Settings,
    missing_handoff: str | None,
) -> None:
    payload = action_payload(migrated_settings, points=("0.55", "0.50", "0.30", "0.20", "0.75"))
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert first.report is not None and first.report.result.position_action is not None
    original = first.report.result.position_action
    payload["case_id"] += "-retry"
    payload["business_identity"] += ":retry"
    if missing_handoff == "security":
        payload["position_action"]["security_event_id"] = "synthetic-missing-security"
    elif missing_handoff == "position":
        payload["position_action"]["execution"]["concentration_event_id"] = (
            "synthetic-missing-position"
        )
    repeated = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert repeated.report is not None and repeated.report.result.position_action is not None
    action = repeated.report.result.position_action
    assert action.anchor == original.anchor
    assert action.target_quantity == 100
    assert action.execution is not None and original.execution is not None
    if missing_handoff:
        assert action.execution.disposition == "BLOCKED"
        assert action.direction == "REDUCE"
        return
    assert action.execution.targets == original.execution.targets


@pytest.mark.parametrize(
    "defect",
    [
        "security",
        "lifecycle",
        "policy",
        "market-state",
        "missing-risk",
        "wrong-owner",
        "stale-assessment",
    ],
)
def test_mismatched_evidence_cannot_authorize_statistical_position_action(
    migrated_settings: Settings,
    defect: str,
) -> None:
    payload = action_payload(migrated_settings)
    command = payload["position_action"]
    if defect == "security":
        command["security_id"] = "XQZ-OTHER"
    elif defect == "lifecycle":
        command["lifecycle_id"] = "synthetic-other-lifecycle"
    elif defect == "policy":
        command["policy"]["value_established_lower"] = "0.60"
    elif defect == "market-state":
        command["qualification_scope"]["market_state"] = "OVERALL"
    elif defect == "missing-risk":
        command["execution"]["stress_event_id"] = "synthetic-unavailable"
    elif defect == "wrong-owner":
        payload["access_scope"]["user_id"] = "synthetic-other-user"
    else:
        command["cutoff_at"] = command["execution"]["cutoff_at"] = payload["knowledge_cutoff"] = (
            "2042-05-19T16:00:00Z"
        )
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None
    action = saved.report.result.position_action
    assert action is not None and action.model_direction is None
    assert action.direction != "HOLD"
    assert action.disposition != "CONCLUDED"


def change_quantity(snapshot: dict[str, Any], quantity: str, *, fee: str = "0") -> None:
    for account in snapshot["accounts"]:
        row = account["positions"][0]
        delta = Decimal(quantity) - Decimal(row["total_quantity"])
        basis_delta = delta * Decimal(9)
        cash_delta = -delta * Decimal(10)
        row.update(
            total_quantity=quantity,
            broker_sellable_quantity=quantity,
            reported_cost_basis=str(Decimal(row["reported_cost_basis"]) + basis_delta),
        )
        cash = account["cash_state"]
        for key in ("ledger_cash", "trading_cash", "transferable_cash"):
            cash[key] = str(Decimal(cash[key]) + cash_delta - Decimal(fee))
        account["account_equity"] = str(Decimal(account["account_equity"]) - Decimal(fee))
        account["ledger_entries"].append(
            {
                "entry_id": (
                    f"synthetic-adjusted-fill:{account['account_id']}:{snapshot['cutoff_at']}"
                ),
                "entry_type": "FILL",
                "security_id": row["security_id"],
                "quantity_delta": str(delta),
                "cost_basis_delta": str(basis_delta),
                "cash_delta": str(cash_delta),
                "occurred_at": snapshot["cutoff_at"].replace("16:00", "15:00"),
                "evidence": position_evidence(
                    "synthetic-later-fill", cutoff_at=snapshot["cutoff_at"]
                ),
            }
        )
        if Decimal(fee):
            account["ledger_entries"].append(
                {
                    "entry_id": f"synthetic-disposal-fee:{account['account_id']}",
                    "entry_type": "FEE",
                    "security_id": row["security_id"],
                    "quantity_delta": "0",
                    "cost_basis_delta": "0",
                    "cash_delta": str(-Decimal(fee)),
                    "occurred_at": snapshot["cutoff_at"].replace("16:00", "15:00"),
                    "evidence": position_evidence(
                        "synthetic-later-fee", cutoff_at=snapshot["cutoff_at"]
                    ),
                }
            )


@pytest.mark.parametrize(
    "quantity,price", [("75", "10"), ("120", "10"), ("40", "10"), ("100", "11")]
)
def test_actual_partial_sale_repurchase_or_below_target_does_not_reset_anchor(
    migrated_settings: Settings,
    quantity: str,
    price: str,
) -> None:
    first_payload = action_payload(
        migrated_settings, points=("0.55", "0.50", "0.30", "0.20", "0.75")
    )
    first = run_frozen_decision_case(
        migrated_settings, first_payload, clock=GovernanceClock(first_payload["knowledge_cutoff"])
    )
    assert first.report is not None and first.report.result.position_action is not None
    # The follow-up uses the saved security report's original thesis and evidence.
    # Obtain the public report identity by replaying the frozen security case's business identity.
    security = replay_default_frozen_decision_case(
        migrated_settings, "synthetic:governance:action-security:2042-05-18T16:00:00Z"
    )
    assert security.report is not None and security.report.result.exit_assessment is not None
    basis = security.report.result.exit_assessment.model_dump(mode="json")

    def later(snapshot: dict[str, Any]) -> None:
        if quantity != "100":
            change_quantity(snapshot, quantity)
        for account in snapshot["accounts"]:
            row = account["positions"][0]
            row["market_price"] = price
            account["account_equity"] = str(
                Decimal(account["account_equity"]) + Decimal(quantity) * (Decimal(price) - 10)
            )

    followup = action_payload(
        migrated_settings,
        cutoff_at="2042-05-19T16:00:00Z",
        previous=basis,
        points=("0.55", "0.50", "0.30", "0.20", "0.75"),
        snapshot_transform=later,
        standard_price=price,
    )
    saved = run_frozen_decision_case(
        migrated_settings, followup, clock=GovernanceClock(followup["knowledge_cutoff"])
    )
    assert saved.report is not None and saved.report.result.position_action is not None
    action = saved.report.result.position_action
    assert action.anchor == first.report.result.position_action.anchor
    assert action.target_quantity == min(Decimal(100), Decimal(quantity) * 2)
    assert action.current_quantity == Decimal(quantity) * 2
    assert action.cost_anchor == first.report.result.position_action.cost_anchor
    assert not action.buy_authorized


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("followup_model", ["REDUCE", "HOLD"])
def test_paid_costs_count_against_first_exit_value_and_do_not_cancel_saved_cap(
    migrated_settings: Settings,
    blocked: bool,
    followup_model: str,
) -> None:
    first_payload = action_payload(
        migrated_settings, points=("0.55", "0.50", "0.30", "0.20", "0.75")
    )
    first = run_frozen_decision_case(
        migrated_settings, first_payload, clock=GovernanceClock(first_payload["knowledge_cutoff"])
    )
    assert first.report is not None and first.report.result.position_action is not None
    security = replay_default_frozen_decision_case(
        migrated_settings, "synthetic:governance:action-security:2042-05-18T16:00:00Z"
    )
    assert security.report is not None and security.report.result.exit_assessment is not None

    def later_snapshot(snapshot: dict[str, Any]) -> None:
        change_quantity(snapshot, "75", fee="9")
        if blocked:
            for account in snapshot["accounts"]:
                account["execution_restrictions"] = [
                    {
                        "restriction_id": "synthetic-followup-block",
                        "security_id": "XQZ-4017",
                        "kind": "NO_BIDS",
                        "reason": "Synthetic unavailable market",
                        "active": True,
                        "evidence": position_evidence(
                            "synthetic-block", cutoff_at=snapshot["cutoff_at"]
                        ),
                    }
                ]

    followup = action_payload(
        migrated_settings,
        cutoff_at="2042-05-19T16:00:00Z",
        previous=security.report.result.exit_assessment.model_dump(mode="json"),
        points=("0.55", "0.50", "0.30", "0.20", "0.75")
        if followup_model == "REDUCE"
        else ("0.20", "0.15", "0.10", "0.05", "0.75"),
        snapshot_transform=later_snapshot,
    )
    saved = run_frozen_decision_case(
        migrated_settings, followup, clock=GovernanceClock(followup["knowledge_cutoff"])
    )
    assert saved.report is not None and saved.report.result.position_action is not None
    action = saved.report.result.position_action
    assert "STATISTICAL_COST_OUT_OF_SCOPE" in action.reasons, (
        first.report.result.position_action.model_dump(mode="json"),
        action.model_dump(mode="json"),
    )
    assert action.direction == "REDUCE" and action.target_quantity == 100
    assert action.cost_anchor == first.report.result.position_action.cost_anchor
    assert action.cost_anchor is not None and action.cost_anchor.market_value == 2000


@pytest.mark.parametrize("missing_cost", [False, True])
def test_first_deterministic_reduction_freezes_cost_anchor_without_statistical_authority(
    migrated_settings: Settings,
    missing_cost: bool,
) -> None:
    payload = action_payload(migrated_settings, normal=False)
    if missing_cost:
        payload["position_action"]["execution"]["routes"][0]["cost_curve"] = None
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None and saved.report.result.position_action is not None
    action = saved.report.result.position_action
    assert action.direction == "REDUCE" and action.model_direction is None
    assert action.anchor is None
    assert action.cost_anchor is not None and action.cost_anchor.market_value == 2000
    assert action.cost_anchor.frozen_at.isoformat() == "2042-05-18T16:00:00+00:00"


def test_quantity_changing_corporate_action_cannot_reuse_an_unadjusted_anchor(
    migrated_settings: Settings,
) -> None:
    payload = action_payload(migrated_settings, points=("0.55", "0.50", "0.30", "0.20", "0.75"))
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert first.report is not None and first.report.result.position_action is not None
    security = replay_default_frozen_decision_case(
        migrated_settings, "synthetic:governance:action-security:2042-05-18T16:00:00Z"
    )
    assert security.report is not None and security.report.result.exit_assessment is not None

    def corporate(snapshot: dict[str, Any]) -> None:
        for account in snapshot["accounts"]:
            row = account["positions"][0]
            row.update(total_quantity="150", broker_sellable_quantity="150")
            account["account_equity"] = str(Decimal(account["account_equity"]) + 500)
            account["ledger_entries"].append(
                {
                    "entry_id": f"synthetic-corporate:{account['account_id']}",
                    "entry_type": "CORPORATE_ACTION",
                    "security_id": row["security_id"],
                    "quantity_delta": "50",
                    "cost_basis_delta": "0",
                    "cash_delta": "0",
                    "occurred_at": "2042-05-19T15:00:00Z",
                    "evidence": position_evidence(
                        "synthetic-corporate", cutoff_at=snapshot["cutoff_at"]
                    ),
                }
            )

    followup = action_payload(
        migrated_settings,
        cutoff_at="2042-05-19T16:00:00Z",
        previous=security.report.result.exit_assessment.model_dump(mode="json"),
        points=("0.55", "0.50", "0.30", "0.20", "0.75"),
        snapshot_transform=corporate,
    )
    saved = run_frozen_decision_case(
        migrated_settings, followup, clock=GovernanceClock(followup["knowledge_cutoff"])
    )
    assert saved.report is not None and saved.report.result.position_action is not None
    action = saved.report.result.position_action
    assert action.model_direction is None
    assert action.execution is not None and action.execution.disposition == "BLOCKED"
    assert "EXECUTION_QUANTITY_BASIS_UNRESOLVED" in action.reasons
    assert action.execution.targets[0].remaining_gap is None
    assert action.execution.targets[0].remaining_quantity is None
    assert action.anchor == first.report.result.position_action.anchor


@pytest.mark.parametrize("business", ["result-failed", "result-abstained"])
def test_business_failure_cannot_cancel_authoritative_exit(
    migrated_settings: Settings,
    business: str,
) -> None:
    from pathlib import Path

    payload = action_payload(migrated_settings, gate="legal")
    source = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/synthetic/result-families" / f"{business}.json"
        ).read_text()
    )
    payload["input"] = {**source["input"], "position_action": payload["position_action"]}
    payload["expected_external_result"] = source["expected_external_result"]
    saved = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert saved.report is not None and saved.report.result.position_action is not None
    assert saved.report.result.position_action.direction == "EXIT"
    assert saved.report.result.position_action.target_quantity == 0


def test_action_command_cannot_supply_its_own_qualified_cap(migrated_settings: Settings) -> None:
    from pydantic import ValidationError

    payload = action_payload(migrated_settings)
    command = deepcopy(payload["position_action"])
    command["execution"]["established_targets"] = [
        {
            "target_id": "synthetic-forged-cap",
            "security_id": command["security_id"],
            "target_quantity": "0",
            "direction": "EXIT",
            "qualified": True,
            "policy_version": command["policy"]["version"]["version_id"],
            "evidence": position_evidence(
                "synthetic-forged-evidence", cutoff_at=command["cutoff_at"]
            ),
        }
    ]
    with pytest.raises(ValidationError, match="committed host evidence"):
        PositionActionCommand.model_validate(command)


def test_saved_action_cli_replay_and_authenticated_report_are_identical(
    migrated_settings: Settings,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    from test_authenticated_report_delivery import _authenticated_client_with_csrf

    from stock_profiler.entrypoints.cli import main
    from stock_profiler.foundation.clock import UtcClock

    payload = action_payload(migrated_settings)
    source = tmp_path / "synthetic-position-action.json"
    source.write_text(json.dumps(payload))
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "api")
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: migrated_settings)
    monkeypatch.setattr(
        UtcClock, "now", lambda self: GovernanceClock(payload["knowledge_cutoff"]).now()
    )
    monkeypatch.setattr(sys, "argv", ["stock-profiler", "decision-case-run", "--case", str(source)])
    main()
    first = json.loads(capsys.readouterr().out)
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
    assert json.loads(capsys.readouterr().out) == first
    settings = migrated_settings.model_copy(
        update={"report_account_ids": tuple(payload["access_scope"]["account_ids"])}
    )
    client, _ = _authenticated_client_with_csrf(settings, monkeypatch)
    response = client.get(f"/api/v1/reports/{first['report_version_id']}")
    assert response.status_code == 200 and response.json() == first["report"]
    assert response.headers["cache-control"] == "no-store"
