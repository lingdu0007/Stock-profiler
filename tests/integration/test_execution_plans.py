from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
from test_drawdown_protection import drawdown_case
from test_issuer_concentration import concentration_authorization_payload, concentration_payload
from test_portfolio_stress import stress_proposal
from test_position_state_reconciliation import (
    position_case_payload,
    position_evidence,
    position_snapshot_command,
    refresh_current_position_evidence,
)
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import FormalReport
from stock_profiler.modules.delivery.access import AccessPrincipal


def execution_payload(settings: Settings) -> dict[str, Any]:
    snapshot = position_snapshot_command()
    payload = position_case_payload(settings, "execution-plan", snapshot)
    del payload["position"]
    payload["version_bundle"].update(
        case_contract_version="execution.1.0.0",
        host_contract_version="execution.1.0.0",
        report_projection_contract_version="execution.1.0.0",
    )
    payload["execution_plan"] = {
        "contract_version": "1.0.0",
        "operation": "EXECUTION_PLAN",
        "portfolio_id": "synthetic-decision-portfolio-alpha",
        "authorization_id": "synthetic-authorization",
        "cutoff_at": snapshot["cutoff_at"],
        "concentration_event_id": "synthetic-missing-concentration",
        "stress_event_id": "synthetic-missing-stress",
        "liquidity_event_id": "synthetic-missing-liquidity",
        "drawdown_event_id": "synthetic-missing-drawdown",
    }
    return payload


def test_missing_committed_risk_handoff_blocks_plan_without_defaulting_to_hold(
    migrated_settings: Settings,
) -> None:
    payload = execution_payload(migrated_settings)
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    plan = execution.report.result.execution_plan
    assert plan is not None
    assert plan.disposition == "BLOCKED"
    assert plan.reasons == ("RISK_HANDOFF_UNAVAILABLE",)
    assert plan.new_exposure_blocked
    assert plan.legs == ()
    assert not plan.risk_restored
    assert (
        execution.report.result.outcome_code == payload["expected_external_result"]["outcome_code"]
    )
    assert (
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
        == execution.report
    )


def committed(settings: Settings, payload: dict[str, Any]) -> FormalReport:
    execution = run_frozen_decision_case(settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    return execution.report


def risk_handoff_payload(
    settings: Settings,
    *,
    multi: bool = False,
    partial: bool = False,
    dated: bool = False,
    normal: bool = False,
    buffer: bool = False,
    capital_state: str | None = None,
    split: bool = False,
) -> dict[str, Any]:
    authorization_payload = concentration_authorization_payload(settings)
    if normal:
        authorization_payload["portfolio"]["proposal"]["risk_budget"]["concentration"] = {
            "target_ratio": "0.30",
            "hard_ratio": "0.40",
        }
    if buffer:
        authorization_payload["portfolio"]["proposal"]["risk_budget"]["concentration"] = {
            "target_ratio": "0.13",
            "hard_ratio": "0.21",
        }
    if dated:
        authorization_payload["portfolio"]["proposal"]["cash_obligations"] = [
            {
                "obligation_id": "synthetic-deadline-obligation",
                "amount": "500",
                "purpose": "synthetic-dated-use",
                "latest_usable_at": "2042-05-20T16:00:00Z",
                "target_account_id": "synthetic-account-8029",
            }
        ]
    authorization_payload["portfolio"]["proposal"]["risk_budget"].update(
        {
            key: stress_proposal(friction="0")["risk_budget"][key]
            for key in ("stress_calculation", "downside_grid")
        }
    )
    authorization = committed(settings, authorization_payload)
    concentration = concentration_payload(
        settings, authorization.event_id, quantity="100", identity="plan-concentration"
    )
    snapshot = concentration["concentration"]["position_snapshot"]
    opening_position = None
    if capital_state is not None:
        opening_position = committed(
            settings, position_case_payload(settings, "plan-opening-position", deepcopy(snapshot))
        )
        snapshot["cutoff_at"] = "2042-05-18T16:00:00Z"
        refresh_current_position_evidence(snapshot, snapshot["cutoff_at"])
        concentration["knowledge_cutoff"] = snapshot["cutoff_at"]
        for cost in concentration["concentration"]["liquidation_costs"]:
            cost["evidence"] = position_evidence(
                "synthetic-current-cost", cutoff_at=snapshot["cutoff_at"]
            )
    if split:
        for account in snapshot["accounts"]:
            account["ledger_entries"][0]["quantity_delta"] = "50"
            account["ledger_entries"].append(
                {
                    "entry_id": f"synthetic-split-{account['account_id']}",
                    "entry_type": "CORPORATE_ACTION",
                    "security_id": "XQZ-4017",
                    "quantity_delta": "50",
                    "cost_basis_delta": "0",
                    "cash_delta": "0",
                    "occurred_at": "2042-05-17T15:00:00Z",
                    "evidence": position_evidence("synthetic-split"),
                }
            )
    if multi:
        for account in snapshot["accounts"]:
            original_position = account["positions"][0]
            original_entry = account["ledger_entries"][0]
            account["positions"] = []
            account["ledger_entries"] = []
            for index in range(5):
                security = f"XQZ-PLAN-{index}"
                row = deepcopy(original_position)
                row.update(
                    position_id=f"synthetic-position-{index}",
                    lifecycle_id=f"synthetic-life-{index}",
                    issuer_id=f"synthetic-issuer-{index}",
                    security_id=security,
                    broker_sellable_quantity="20" if partial else "100",
                    frozen_quantity="80" if partial else "0",
                )
                account["positions"].append(row)
                entry = deepcopy(original_entry)
                entry.update(entry_id=f"synthetic-entry-{index}", security_id=security)
                account["ledger_entries"].append(entry)
            account["cash_state"].update(
                opening_ledger_cash="4500", ledger_cash="0", trading_cash="0", transferable_cash="0"
            )
            account["account_equity"] = "5000"
    concentration_report = committed(settings, concentration)
    assert concentration_report.result.concentration is not None
    assert concentration_report.result.concentration.disposition == "ASSESSED"
    position_report = committed(
        settings, position_case_payload(settings, "plan-position", snapshot)
    )
    refs = {"concentration": concentration_report.event_id}
    for name, version, command in (
        (
            "stress",
            "8.0.0",
            {
                "operation": "PORTFOLIO_STRESS_ASSESS",
                "contract_version": "1.0.0",
                "portfolio_id": "synthetic-decision-portfolio-alpha",
                "authorization_id": authorization.event_id,
                "position_snapshot": snapshot,
            },
        ),
        (
            "liquidity",
            "8.2.0",
            {
                "operation": "LIQUIDITY_ASSESS",
                "contract_version": "1.0.0",
                "synthetic": True,
                "generator_version": "execution-journey-v1",
                "seed": 8113,
                "portfolio_id": "synthetic-decision-portfolio-alpha",
                "authorization_id": authorization.event_id,
                "position_snapshot": snapshot,
                "expected_purchase_fees": "0",
                "expected_liquidation_fees": "0",
                "sale_terms": [
                    {
                        "account_id": account["account_id"],
                        "security_id": row["security_id"],
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
                    for row in account["positions"]
                ],
                "transfer_routes": [],
                "settled_coverage": [],
                "cost_evidence": snapshot["snapshot_evidence"],
            },
        ),
    ):
        payload = position_case_payload(settings, f"plan-{name}", snapshot)
        del payload["position"]
        payload["version_bundle"].update(
            case_contract_version=version,
            host_contract_version=version,
            report_projection_contract_version=version,
        )
        payload[name] = command
        report = committed(settings, payload)
        refs[name] = report.event_id
        if name == "stress":
            assert report.result.stress is not None
            assert report.result.stress.state == ("HARD_BREACH" if multi else "NORMAL")
        else:
            assert report.result.liquidity is not None
            assert report.result.liquidity.disposition == (
                "REMEDIATION_REQUIRED" if multi else "AVAILABLE"
            )
    capital = committed(
        settings,
        drawdown_case(
            settings,
            "plan-capital",
            {
                "operation": "OPEN",
                "contract_version": "1.0.0",
                "portfolio_id": "synthetic-decision-portfolio-alpha",
                "authorization_id": authorization.event_id,
                "epoch_id": "synthetic-plan-capital",
                "previous_decision_id": None,
                "cutoff_at": "2042-05-17T16:00:00Z" if opening_position else snapshot["cutoff_at"],
                "valuation": {
                    "position_event_id": opening_position.event_id
                    if opening_position
                    else position_report.event_id,
                    "liquidation_cost": "0",
                    "evidence": position_evidence("synthetic-capital-opening")
                    if opening_position
                    else snapshot["snapshot_evidence"],
                },
                "policy": {
                    "synthetic": True,
                    "generator_version": "execution-journey-v1",
                    "seed": 8113,
                    "version_id": "synthetic-plan-capital-policy",
                    "defensive_exposure_ratio": "0.27",
                    "caution_recovery_ratio": "0.07",
                    "defensive_recovery_ratio": "0.09",
                    "caution_recovery_sessions": 2,
                    "defensive_recovery_sessions": 3,
                    "cooling_sessions": 4,
                    "market_calendar_version_id": "synthetic-market-calendar-v1",
                },
            },
            account_ids=[account["account_id"] for account in snapshot["accounts"]],
        ),
    )
    assert capital.result.drawdown is not None
    assert capital.result.drawdown.disposition == "ACCEPTED"
    if capital_state is not None:
        assert capital.result.drawdown.state is not None
        capital = committed(
            settings,
            drawdown_case(
                settings,
                "plan-capital-observation",
                {
                    "operation": "OBSERVE",
                    "contract_version": "1.0.0",
                    "portfolio_id": "synthetic-decision-portfolio-alpha",
                    "authorization_id": authorization.event_id,
                    "epoch_id": "synthetic-plan-capital",
                    "previous_decision_id": capital.event_id,
                    "cutoff_at": snapshot["cutoff_at"],
                    "policy": capital.result.drawdown.state.policy.model_dump(mode="json"),
                    "valuation": {
                        "position_event_id": position_report.event_id,
                        "liquidation_cost": {"CAUTION": "1200", "PRESERVATION": "2200"}[
                            capital_state
                        ],
                        "evidence": snapshot["snapshot_evidence"],
                    },
                },
                account_ids=[account["account_id"] for account in snapshot["accounts"]],
            ),
        )
        assert (
            capital.result.drawdown is not None
            and capital.result.drawdown.disposition == "ACCEPTED"
        )
        assert (
            capital.result.drawdown.state is not None
            and capital.result.drawdown.state.risk_state == capital_state
        )
    refs["drawdown"] = capital.event_id
    payload = execution_payload(settings)
    payload["knowledge_cutoff"] = snapshot["cutoff_at"]
    payload["execution_plan"].update(
        cutoff_at=snapshot["cutoff_at"],
        authorization_id=authorization.event_id,
        **{f"{name}_event_id": event_id for name, event_id in refs.items()},
    )
    return deepcopy(payload)


def test_committed_concentration_target_survives_missing_execution_cost_evidence(
    migrated_settings: Settings,
) -> None:
    report = committed(migrated_settings, risk_handoff_payload(migrated_settings))
    plan = report.result.execution_plan
    assert plan is not None
    assert plan.disposition == "BLOCKED"
    assert plan.reasons == ("EXECUTION_COST_EVIDENCE_FAILED",)
    assert plan.targets[0].security_id == "XQZ-4017"
    assert plan.targets[0].target_quantity == Decimal("130")
    assert plan.targets[0].required_sale_quantity == Decimal("70")
    assert plan.new_exposure_blocked
    assert not plan.risk_restored


def test_valid_concentration_obligation_survives_an_unavailable_other_risk_handoff(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(migrated_settings)
    payload["execution_plan"]["stress_event_id"] = "synthetic-missing-stress"
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None and plan.disposition == "BLOCKED"
    assert len(plan.targets) == 1
    assert plan.targets[0].target_quantity == Decimal("130")
    assert plan.targets[0].required_sale_quantity is None
    assert plan.legs == ()


def test_independent_zero_target_survives_missing_risk_evidence(
    migrated_settings: Settings,
) -> None:
    payload = execution_payload(migrated_settings)
    payload["execution_plan"]["established_targets"] = [
        {
            "target_id": "synthetic-zero",
            "security_id": "XQZ-4017",
            "target_quantity": "0",
            "direction": "EXIT",
            "qualified": True,
            "policy_version": "synthetic-policy",
            "evidence": position_evidence("synthetic-zero"),
        }
    ]
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None and plan.disposition == "BLOCKED"
    assert len(plan.targets) == 1 and plan.targets[0].target_quantity == 0
    assert plan.targets[0].direction == "EXIT"


def test_future_history_does_not_contaminate_an_earlier_denial(migrated_settings: Settings) -> None:
    original = risk_handoff_payload(migrated_settings)
    committed(migrated_settings, original)
    earlier = execution_payload(migrated_settings)
    earlier["case_id"] += "-earlier"
    earlier["business_identity"] += ":earlier"
    earlier["knowledge_cutoff"] = "2042-05-16T16:00:00Z"
    earlier["execution_plan"]["cutoff_at"] = earlier["knowledge_cutoff"]
    plan = committed(migrated_settings, earlier).result.execution_plan
    assert plan is not None and plan.disposition == "BLOCKED"
    assert plan.targets == ()
    assert plan.reasons == ("EXECUTION_SNAPSHOT_NOT_FORWARD",)


def test_narrow_scope_denial_cannot_seed_a_later_full_scope_target(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(migrated_settings)
    payload["execution_plan"]["routes"] = execution_routes()
    original = committed(migrated_settings, payload).result.execution_plan
    assert original is not None
    narrow = deepcopy(payload)
    narrow["case_id"] += "-narrow"
    narrow["business_identity"] += ":narrow"
    narrow["access_scope"]["account_ids"] = ["synthetic-account-4017"]
    narrow["execution_plan"]["established_targets"] = [
        {
            "target_id": "synthetic-invalid-scope-zero",
            "security_id": "XQZ-4017",
            "target_quantity": "0",
            "direction": "EXIT",
            "qualified": True,
            "policy_version": "synthetic-policy",
            "evidence": position_evidence("synthetic-zero"),
        }
    ]
    denied = committed(migrated_settings, narrow).result.execution_plan
    assert denied is not None
    assert denied.reasons == ("EXECUTION_HISTORY_SCOPE_INCOMPLETE",)
    assert denied.targets == ()
    payload["case_id"] += "-restored"
    payload["business_identity"] += ":restored"
    restored = committed(migrated_settings, payload).result.execution_plan
    assert restored is not None and restored.targets == original.targets


def test_quantity_changing_corporate_action_blocks_unadjusted_historical_caps(
    migrated_settings: Settings,
) -> None:
    first = execution_payload(migrated_settings)
    first["knowledge_cutoff"] = "2042-05-16T16:00:00Z"
    first["execution_plan"]["cutoff_at"] = first["knowledge_cutoff"]
    first["execution_plan"]["established_targets"] = [
        {
            "target_id": "synthetic-anchor",
            "security_id": "XQZ-4017",
            "target_quantity": "50",
            "direction": "REDUCE",
            "qualified": True,
            "policy_version": "synthetic-policy",
            "evidence": position_evidence("synthetic-anchor", cutoff_at=first["knowledge_cutoff"]),
        }
    ]
    committed(migrated_settings, first)
    later = risk_handoff_payload(migrated_settings, split=True)
    later["case_id"] += "-split"
    later["business_identity"] += ":split"
    later["execution_plan"]["routes"] = execution_routes()
    plan = committed(migrated_settings, later).result.execution_plan
    assert plan is not None and plan.disposition == "BLOCKED"
    assert plan.reasons == ("EXECUTION_QUANTITY_BASIS_UNRESOLVED",)
    assert plan.legs == ()
    assert plan.targets[0].required_sale_quantity is None


def test_cost_failure_keeps_qualified_portfolio_gaps(migrated_settings: Settings) -> None:
    plan = committed(
        migrated_settings, risk_handoff_payload(migrated_settings, multi=True)
    ).result.execution_plan
    assert plan is not None and plan.disposition == "BLOCKED"
    assert plan.projected_stress_gap == Decimal("900")
    assert plan.projected_cash_gap == Decimal("3900")
    assert any(
        "stress" in source for target in plan.targets for source in target.source_obligation_ids
    )


def test_normal_no_action_does_not_invent_an_exposure_prohibition(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(migrated_settings, normal=True)
    payload["execution_plan"]["routes"] = execution_routes()
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None and plan.disposition == "PLANNED"
    assert not plan.new_exposure_blocked
    assert plan.legs == ()
    assert not plan.requires_confirmation


@pytest.mark.parametrize("gate", ["concentration-buffer", "capital-caution"])
def test_non_selling_risk_gate_keeps_the_exposure_prohibition(
    migrated_settings: Settings, gate: str
) -> None:
    payload = risk_handoff_payload(
        migrated_settings,
        normal=gate == "capital-caution",
        buffer=gate == "concentration-buffer",
        capital_state="CAUTION" if gate == "capital-caution" else None,
    )
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None and plan.disposition == "PLANNED"
    assert plan.new_exposure_blocked
    assert plan.legs == () and not plan.requires_confirmation


@pytest.mark.parametrize("missing_stress", [False, True])
def test_capital_preservation_zero_survives_conjunction_and_missing_handoffs(
    migrated_settings: Settings,
    missing_stress: bool,
) -> None:
    payload = risk_handoff_payload(migrated_settings, capital_state="PRESERVATION")
    routes = execution_routes()
    for route in routes:
        route["rules_evidence"] = position_evidence(
            "synthetic-current-rules", cutoff_at=payload["knowledge_cutoff"]
        )
        route["cost_curve"]["evidence"] = position_evidence(
            "synthetic-current-cost", cutoff_at=payload["knowledge_cutoff"]
        )
    payload["execution_plan"]["routes"] = routes
    if missing_stress:
        payload["execution_plan"]["stress_event_id"] = "synthetic-missing-stress"
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None
    assert plan.targets[0].target_quantity == 0 and plan.targets[0].direction == "EXIT"
    assert plan.new_exposure_blocked and not plan.risk_restored
    assert plan.disposition == ("BLOCKED" if missing_stress else "PLANNED")
    assert len(plan.legs) == (0 if missing_stress else 2)


def test_capital_caution_survives_dated_cash_waterfall_reallocation(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(
        migrated_settings, normal=True, dated=True, capital_state="CAUTION"
    )
    routes = execution_routes()
    for route in routes:
        route["rules_evidence"] = position_evidence(
            "synthetic-current-rules", cutoff_at=payload["knowledge_cutoff"]
        )
        route["cost_curve"]["evidence"] = position_evidence(
            "synthetic-current-cost", cutoff_at=payload["knowledge_cutoff"]
        )
    payload["execution_plan"]["routes"] = routes
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None and plan.disposition == "PLANNED"
    assert plan.projected_cash_gap == 0 and plan.legs
    assert plan.new_exposure_blocked


def test_waterfall_preserves_a_separate_rounding_induced_full_sale(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(migrated_settings, multi=True)
    routes = []
    for index in range(5):
        for route in execution_routes():
            route["security_id"] = f"XQZ-PLAN-{index}"
            route["cost_curve"].update(commission_ratio="0.30", minimum_commission="0")
            if index == 0:
                route.update(minimum_quantity="100", quantity_increment="100")
            routes.append(route)
    payload["execution_plan"]["routes"] = routes
    payload["execution_plan"]["established_targets"] = [
        {
            "target_id": "synthetic-reduction",
            "security_id": "XQZ-PLAN-0",
            "target_quantity": "60",
            "direction": "REDUCE",
            "qualified": True,
            "policy_version": "synthetic-policy",
            "evidence": position_evidence("synthetic-target"),
        }
    ]
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None and plan.disposition == "RECONFIRMATION_REQUIRED"
    target = plan.targets[0]
    assert target.target_quantity == 60 and target.direction == "REDUCE"
    assert target.rounding_induced_full_sale
    assert plan.projected_cash_gap == 0


def test_fallback_for_one_security_does_not_override_other_verified_costs(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(migrated_settings, multi=True)
    routes = []
    for index in range(5):
        for route in execution_routes():
            route["security_id"] = f"XQZ-PLAN-{index}"
            if index == 0:
                route["conservative_cost_curve"] = route["cost_curve"]
                route["cost_curve"] = None
            routes.append(route)
    payload["execution_plan"]["routes"] = routes
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None and plan.disposition == "PLANNED"
    assert all(
        leg.account_id == "synthetic-account-8029"
        for leg in plan.legs
        if leg.security_id != "XQZ-PLAN-0"
    )


@pytest.mark.parametrize("partial", [False, True])
def test_waterfall_credits_existing_targets_before_proportional_remaining_sales(
    migrated_settings: Settings,
    partial: bool,
) -> None:
    payload = risk_handoff_payload(migrated_settings, multi=True, partial=partial)
    routes = []
    for index in range(5):
        for route in execution_routes():
            route["security_id"] = f"XQZ-PLAN-{index}"
            route["cost_curve"]["commission_ratio"] = "0"
            route["cost_curve"]["minimum_commission"] = "0"
            routes.append(route)
    payload["execution_plan"]["routes"] = routes
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None
    assert len(plan.targets) == 5
    assert plan.initial_target_sale_value == Decimal("2000" if partial else "3500")
    assert sum((leg.gross_proceeds for leg in plan.legs), Decimal(0)) == Decimal(
        "2000" if partial else "4000"
    ), "PROTECTION_CONTRACT_RESTORATION_WATERFALL"
    assert plan.projected_stress_gap == Decimal("440" if partial else "0")
    assert plan.projected_cash_gap == Decimal("1900" if partial else "0")
    assert plan.disposition == ("BLOCKED" if partial else "PLANNED")
    assert (
        all(
            target.remaining_gap is not None and target.remaining_gap > 0 for target in plan.targets
        )
        if partial
        else all(target.remaining_gap == 0 for target in plan.targets)
    )
    assert plan.new_exposure_blocked
    assert not plan.risk_restored


def test_missing_costs_can_use_an_evidenced_conservative_bound_without_claiming_optimality(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(migrated_settings)
    routes = execution_routes()
    for route in routes:
        bound = deepcopy(route["cost_curve"])
        bound.update(
            version_id="synthetic-cost-bound", commission_ratio="0.02", minimum_commission="0"
        )
        route["conservative_cost_curve"] = bound
        route["cost_curve"] = None
    payload["execution_plan"]["routes"] = routes
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None and plan.disposition == "PLANNED"
    assert tuple(leg.quantity for leg in plan.legs) == (Decimal("30"), Decimal("40"))
    assert sum((leg.disposal_cost for leg in plan.legs), Decimal(0)) == Decimal("14")
    assert plan.cost_routing_basis == "CONSERVATIVE_BOUND_PROPORTIONAL"


def test_late_transferable_proceeds_cannot_hide_a_dated_account_funding_gap(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(migrated_settings, dated=True)
    routes = execution_routes()
    for route in routes:
        route["transferable_at"] = "2042-05-21T16:00:00Z"
    payload["execution_plan"]["routes"] = routes
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None and plan.disposition == "BLOCKED"
    assert plan.projected_cash_gap == Decimal("500")
    assert plan.new_exposure_blocked
    assert sum((leg.quantity for leg in plan.legs), Decimal(0)) == Decimal("70")


def test_account_funding_obligation_precedes_cost_routing_at_the_same_legal_quantity(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(migrated_settings, dated=True)
    routes = execution_routes()
    routes[0]["cost_curve"].update(commission_ratio="0", minimum_commission="0")
    payload["execution_plan"]["routes"] = routes
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None and plan.disposition == "PLANNED"
    assert tuple(leg.quantity for leg in plan.legs) == (Decimal("10"), Decimal("60"))
    assert plan.projected_cash_gap == 0


def test_a_plan_never_releases_the_existing_target_on_replay_with_a_wider_target(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(migrated_settings)
    payload["execution_plan"]["routes"] = execution_routes()
    payload["execution_plan"]["established_targets"] = [
        {
            "target_id": "synthetic-persistent-target",
            "security_id": "XQZ-4017",
            "target_quantity": "50",
            "direction": "REDUCE",
            "qualified": True,
            "policy_version": "synthetic-target-policy",
            "evidence": position_evidence("synthetic-target-evidence"),
        }
    ]
    original = committed(migrated_settings, payload)
    later = deepcopy(payload)
    later["business_identity"] += ":new-attempt"
    later["case_id"] += "-new-attempt"
    later["execution_plan"]["established_targets"] = []
    report = committed(migrated_settings, later)
    assert report.result.execution_plan is not None
    assert report.result.execution_plan.targets[0].target_quantity == Decimal("50")
    assert original.result.execution_plan is not None
    assert original.result.execution_plan.targets[0].target_quantity == Decimal("50")
    missing = deepcopy(later)
    missing["business_identity"] += ":missing"
    missing["case_id"] += "-missing"
    missing["execution_plan"]["stress_event_id"] = "synthetic-missing-stress"
    blocked = committed(migrated_settings, missing).result.execution_plan
    assert blocked is not None and blocked.disposition == "BLOCKED"
    assert blocked.targets[0].target_quantity == Decimal("50")
    assert blocked.targets[0].required_sale_quantity is None
    assert blocked.legs == ()


def execution_routes() -> list[dict[str, Any]]:
    return [
        {
            "account_id": account,
            "security_id": "XQZ-4017",
            "minimum_quantity": "10",
            "quantity_increment": "10",
            "allow_full_odd_lot": True,
            "first_sellable_at": "2042-05-18T16:00:00Z",
            "transferable_at": "2042-05-19T16:00:00Z",
            "rules_evidence": position_evidence("synthetic-execution-rules"),
            "cost_curve": {
                "version_id": f"synthetic-cost-{index}",
                "commission_ratio": ratio,
                "minimum_commission": minimum,
                "trading_fee_ratio": "0",
                "settlement_fee_ratio": "0",
                "tax_ratio": "0",
                "slippage_ratio": "0",
                "market_impact_ratio": "0",
                "dividend_lots": [],
                "evidence": position_evidence("synthetic-execution-cost"),
            },
        }
        for index, (account, ratio, minimum) in enumerate(
            (
                ("synthetic-account-4017", "0.001", "9"),
                ("synthetic-account-8029", "0.01", "1"),
            )
        )
    ]


def test_route_minimizes_full_cost_including_minimum_commission(
    migrated_settings: Settings,
) -> None:
    payload = risk_handoff_payload(migrated_settings)
    payload["execution_plan"]["routes"] = execution_routes()
    report = committed(migrated_settings, payload)
    plan = report.result.execution_plan
    assert plan is not None and plan.disposition == "PLANNED"
    assert len(plan.legs) == 1
    assert plan.legs[0].account_id == "synthetic-account-8029"
    assert plan.legs[0].quantity == Decimal("70")
    assert plan.legs[0].disposal_cost == Decimal("7")
    assert plan.targets[0].remaining_quantity == Decimal("130")
    assert plan.targets[0].remaining_gap == 0
    assert plan.new_exposure_blocked
    assert not plan.risk_restored
    assert (
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
        == report
    )
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


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("earlier", (("synthetic-account-4017", "70"),)),
        ("equal", (("synthetic-account-4017", "30"), ("synthetic-account-8029", "40"))),
        ("zero", (("synthetic-account-4017", "100"), ("synthetic-account-8029", "100"))),
        (
            "full-sale-rounding",
            (("synthetic-account-4017", "100"), ("synthetic-account-8029", "100")),
        ),
    ],
)
def test_strict_targets_windows_and_legal_units_preserve_action_meaning(
    migrated_settings: Settings,
    mode: str,
    expected: tuple[tuple[str, str], ...],
) -> None:
    payload = risk_handoff_payload(migrated_settings)
    routes = execution_routes()
    if mode == "earlier":
        routes[1]["first_sellable_at"] = routes[1]["transferable_at"]
    elif mode == "equal":
        routes[1]["cost_curve"] = deepcopy(routes[0]["cost_curve"])
        for route in routes:
            route["cost_curve"]["minimum_commission"] = "0"
    else:
        payload["execution_plan"]["established_targets"] = [
            {
                "target_id": "synthetic-established-target",
                "security_id": "XQZ-4017",
                "target_quantity": "0" if mode == "zero" else "60",
                "direction": "EXIT" if mode == "zero" else "REDUCE",
                "qualified": True,
                "evidence": position_evidence("synthetic-target-handoff"),
                "policy_version": "synthetic-target-policy-v1",
            }
        ]
        if mode == "full-sale-rounding":
            for route in routes:
                route.update(minimum_quantity="100", quantity_increment="100")
    payload["execution_plan"]["routes"] = routes
    plan = committed(migrated_settings, payload).result.execution_plan
    assert plan is not None
    assert tuple((leg.account_id, leg.quantity) for leg in plan.legs) == tuple(
        (account, Decimal(quantity)) for account, quantity in expected
    )
    if mode == "full-sale-rounding":
        assert plan.disposition == "RECONFIRMATION_REQUIRED", (
            "PROTECTION_CONTRACT_ROUNDING_CONFIRMATION"
        )
        assert plan.targets[0].direction == "REDUCE"
        assert plan.targets[0].target_quantity == Decimal("60")
        assert plan.targets[0].rounding_induced_full_sale
    elif mode == "zero":
        assert plan.targets[0].target_quantity == 0
        assert plan.targets[0].direction == "EXIT"
        assert not plan.targets[0].rounding_induced_full_sale
    assert plan.requires_confirmation
    assert plan.new_exposure_blocked
    assert not plan.risk_restored
