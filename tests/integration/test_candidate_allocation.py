"""Allocation behavior through the frozen case and committed-report boundary."""

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
from test_execution_plans import execution_payload
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.portfolio.allocation_contracts import CandidateAllocationCommand


def allocation_payload(settings: Settings) -> dict[str, Any]:
    payload = execution_payload(settings)
    risk = payload.pop("execution_plan")
    version = "candidate-allocation.1.0.0"
    payload["version_bundle"].update(
        case_contract_version=version,
        host_contract_version=version,
        report_projection_contract_version=version,
    )
    payload["candidate_allocation"] = {
        "contract_version": "1.0.0",
        "operation": "CANDIDATE_ALLOCATION",
        "cutoff_at": payload["knowledge_cutoff"],
        "risk_handoff": risk,
        "candidate_event_id": "synthetic-missing-candidates",
    }
    payload["candidate_allocation"] = CandidateAllocationCommand.model_validate(
        payload["candidate_allocation"]
    ).model_dump(mode="json")
    payload["input"]["candidate_allocation"] = payload["candidate_allocation"]
    return payload


def test_missing_saved_candidate_handoff_cannot_create_an_allocation(
    migrated_settings: Settings,
) -> None:
    payload = allocation_payload(migrated_settings)
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    plan = execution.report.result.candidate_allocation
    assert plan is not None and plan.disposition == "BLOCKED"
    assert plan.reasons == ("CANDIDATE_HANDOFF_UNAVAILABLE",)
    assert plan.rows == () and not plan.actionable
    assert (
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
        == execution.report
    )


def test_allocation_reaches_issuer_target_and_keeps_the_remainder_as_cash(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None
    plan = report.result.candidate_allocation
    assert plan is not None and plan.disposition == "PLANNED"
    assert plan.rows[0].principal == 700
    assert plan.rows[0].outcome == "FULLY_ALLOCATED"
    assert plan.rows[0].candidate == source.members[0]
    assert plan.total_principal == 700
    assert plan.remaining_cash == 3400
    cash_check = next(check for check in plan.capacity_checks if check.gate_id == "global:cash")
    assert cash_check.available_before == 4100 and cash_check.remaining_after_plan == 3400
    stress_check = next(check for check in plan.capacity_checks if check.gate_id == "global:stress")
    assert stress_check.remaining_after_plan == Decimal(779)
    assert all(check.remaining_after_plan >= 0 for check in plan.capacity_checks)
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
    payload["candidate_allocation"]["routes"][0]["minimum_commission"] = "9999"
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    assert (
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
        == report
    )


def test_turnover_capacity_and_fees_produce_a_partial_allocation(
    migrated_settings: Settings,
) -> None:
    from copy import deepcopy

    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    command["securities"][0]["median_turnover"] = "10000"
    command["routes"][0]["minimum_commission"] = "10"
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.rows[0].principal == 200
    assert plan.rows[0].outcome == "PARTIALLY_ALLOCATED"
    assert "LIQUIDITY_CAPACITY_EXHAUSTED" in plan.rows[0].reasons
    assert plan.rows[0].candidate == source.members[0]
    assert plan.remaining_cash == 3890


@pytest.mark.parametrize(
    "defect", ["permission", "price_tick", "price_evidence", "correlation", "expired_window"]
)
def test_evidence_or_route_defects_preserve_zero_allocation_candidates(
    migrated_settings: Settings,
    defect: str,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    if defect == "permission":
        command["routes"][0]["permission"] = False
    elif defect == "price_tick":
        command["routes"][0]["price_cap"] = "10.5"
    elif defect == "price_evidence":
        command["routes"][0]["evidence"]["source_version"] = None
    elif defect == "correlation":
        command["correlations"]["returns"].pop("FICTIONAL-ORBITAL-MOSAIC")
    else:
        command["securities"][0]["evidence"]["expires_at"] = "2042-05-17T15:00:00Z"
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    row = report.result.candidate_allocation.rows[0]
    assert row.principal == 0 and row.outcome == "UNALLOCATED"
    assert row.reasons and row.candidate == source.members[0]
    assert row.legs == ()


def test_missing_confirmed_price_cap_stops_before_order_quantities(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    command["routes"][0]["price_cap"] = None
    command["routes"][0]["price_cap_confirmed"] = False
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "AWAITING_PRICE_CAP" and plan.total_principal == 0
    assert plan.rows[0].continuous_principal == 700 and plan.rows[0].legs == ()
    assert plan.rows[0].candidate == source.members[0]


def test_existing_issuer_above_entry_target_does_not_receive_more_exposure(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    command["securities"][0]["issuer_id"] = "FICTIONAL-ORBITAL-MOSAIC"
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    row = report.result.candidate_allocation.rows[0]
    assert row.committed_exposure == 2000 and row.target_gap == 0 and row.principal == 0
    assert row.candidate == source.members[0] and "ENTRY_TARGET_REACHED" in row.reasons


def test_existing_neighborhood_breach_does_not_block_an_uncorrelated_purchase(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    command["policy"]["neighborhood_ratio"] = "0.18"
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "PLANNED" and plan.total_principal == 700
    assert plan.rows[0].candidate == source.members[0]


def test_missing_uncommitted_peer_history_closes_only_that_candidate(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings, candidate_count=2)
    del payload["candidate_allocation"]["correlations"]["returns"]["fictional-new-issuer-1"]
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert [row.principal for row in plan.rows] == [700, 0]
    assert tuple(row.candidate for row in plan.rows) == source.members


@pytest.mark.parametrize("shift_days", [1, 365])
def test_return_window_must_be_the_latest_complete_trading_sessions(
    migrated_settings: Settings, shift_days: int
) -> None:
    from datetime import date, timedelta

    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    correlations = payload["candidate_allocation"]["correlations"]
    correlations["market_dates"] = [
        (date.fromisoformat(day) - timedelta(days=shift_days)).isoformat()
        for day in correlations["market_dates"]
    ]
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    row = report.result.candidate_allocation.rows[0]
    assert row.principal == 0 and "CORRELATION_EVIDENCE_FAILED" in row.reasons
    assert row.candidate == source.members[0]


def test_codes_for_one_issuer_share_one_target(migrated_settings: Settings) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings, candidate_count=2)
    command = payload["candidate_allocation"]
    command["securities"][1]["issuer_id"] = "fictional-new-issuer"
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == 700
    assert sorted(row.continuous_principal for row in plan.rows) == [350, 350]
    assert sorted(row.principal for row in plan.rows) == [300, 400]
    assert tuple(row.candidate for row in plan.rows) == source.members


@pytest.mark.parametrize("relationship", ["low", "zero", "negative", "high"])
def test_only_high_positive_correlation_tightens_capacity(
    migrated_settings: Settings, relationship: str
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    held = command["correlations"]["returns"]["FICTIONAL-ORBITAL-MOSAIC"]
    if relationship == "high":
        command["correlations"]["returns"]["fictional-new-issuer"] = held
    elif relationship == "negative":
        command["correlations"]["returns"]["fictional-new-issuer"] = [
            str(-int(value)) for value in held
        ]
    elif relationship == "zero":
        command["correlations"]["returns"]["fictional-new-issuer"] = [
            str((index // 5) % 2) for index in range(120)
        ]
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    row = report.result.candidate_allocation.rows[0]
    assert row.principal == (400 if relationship == "high" else 700)
    assert row.candidate == source.members[0]


def test_accepted_unfilled_buys_consume_target_cash_and_stress(migrated_settings: Settings) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    command["commitments"] = [
        {
            "commitment_id": "synthetic-accepted-unfilled",
            "account_id": "synthetic-account-4017",
            "security_id": "SYNTH-CANDIDATE",
            "issuer_id": "fictional-new-issuer",
            "broker_order_id": None,
            "principal": "300",
            "quantity": "30",
            "price_cap": "10",
            "purchase_cost": "20",
            "disposal_friction": "5",
            "evidence": deepcopy(command["securities"][0]["evidence"]),
        }
    ]
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.rows[0].committed_exposure == 300 and plan.total_principal == 400
    assert plan.remaining_cash == 3380 and plan.rows[0].candidate == source.members[0]


def test_shared_cash_is_not_overdrawn_to_pay_minimum_acquisition_fees(
    migrated_settings: Settings,
) -> None:
    from copy import deepcopy

    from synthetic_candidate_allocation import feasible_payload

    payload, _ = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    command["policy"]["entry_target_ratio"] = "0.30"
    command["policy"]["neighborhood_ratio"] = "0.35"
    command["routes"][0]["minimum_commission"] = "2200"
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == 1900
    assert plan.remaining_cash == 0
    assert plan.rows[0].outcome == "PARTIALLY_ALLOCATED"


def test_disposal_friction_uses_the_normal_stress_capacity(migrated_settings: Settings) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    command["policy"].update(entry_target_ratio="0.30", neighborhood_ratio="0.35")
    command["routes"][0]["disposal_friction_ratio"] = "0.77"
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    row = report.result.candidate_allocation.rows[0]
    assert row.continuous_principal == 940 and row.principal == 900
    assert row.outcome == "PARTIALLY_ALLOCATED" and row.candidate == source.members[0]
    assert "STRESS_CAPACITY_EXHAUSTED" in row.reasons


@pytest.mark.parametrize("large_first_unit", [False, True])
def test_discrete_plan_matches_an_independent_exact_lexicographic_oracle(
    migrated_settings: Settings,
    large_first_unit: bool,
) -> None:
    from decimal import Decimal
    from itertools import product

    from synthetic_candidate_allocation import feasible_payload

    probabilities = ("0.81", "0.72", "0.93")
    payload, source = feasible_payload(
        migrated_settings, candidate_count=3, probabilities=probabilities
    )
    command = payload["candidate_allocation"]
    command["policy"]["entry_target_ratio"] = "0.09"
    minimums = [900, 100, 100] if large_first_unit else [900, 900, 900]
    for route, minimum in zip(command["routes"], minimums, strict=True):
        route["minimum_quantity"] = str(minimum // 10)
    payload["input"]["candidate_allocation"] = deepcopy(command)
    feasible = [
        amounts
        for amounts in product(*[[0, *range(unit, 901, 100)] for unit in minimums])
        if sum(amounts) <= 2400
    ]

    def objective(amounts: tuple[int, ...]) -> tuple[Any, ...]:
        covered = [index for index, amount in enumerate(amounts) if amount]
        return (
            len(covered),
            tuple(sorted(Decimal(amounts[index]) / 800 for index in covered)),
            sum(amounts),
            tuple(sorted(Decimal(probabilities[index]) for index in covered)),
            -len(covered),
            amounts,
        )

    expected = max(feasible, key=objective)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert tuple(row.continuous_principal for row in plan.rows) == (Decimal(800),) * 3
    assert tuple(row.principal for row in plan.rows) == expected
    assert tuple(row.candidate for row in plan.rows) == source.members


def test_direct_overlapping_neighborhoods_do_not_become_transitive_groups(
    migrated_settings: Settings,
) -> None:
    from decimal import Decimal

    from synthetic_candidate_allocation import feasible_payload

    payload, _ = feasible_payload(migrated_settings, candidate_count=4)
    command = payload["candidate_allocation"]
    command["policy"]["entry_target_ratio"] = "0.09"
    basis_a = [Decimal((index % 3) - 1) for index in range(120)]
    basis_b = [
        Decimal(1 if (index // 3) % 2 else -1) * Decimal("0.81649658") for index in range(120)
    ]
    angles = [
        ("1", "0"),
        ("0.90630779", "0.42261826"),
        ("0.64278761", "0.76604444"),
        ("0.25881905", "0.96592583"),
    ]
    for security, (cosine, sine) in zip(command["securities"], angles, strict=True):
        command["correlations"]["returns"][security["issuer_id"]] = [
            str(Decimal(cosine) * a + Decimal(sine) * b)
            for a, b in zip(basis_a, basis_b, strict=True)
        ]
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert tuple(row.principal for row in plan.rows) == (Decimal(800),) * 4
    assert plan.total_principal == 3200


@pytest.mark.parametrize(
    "scenario",
    [
        "SYNTHETIC_INPUT_REJECTED",
        "SYNTHETIC_RESULT_ABSTAINED",
        "SYNTHETIC_RESULT_FAILED",
        "SYNTHETIC_RESULT_PENDING",
        "SYNTHETIC_RESULT_EXPIRED",
        "SYNTHETIC_RESULT_EXECUTION_BLOCKED",
        "SYNTHETIC_RESULT_UNKNOWN",
    ],
)
def test_non_successful_business_outcome_cannot_form_a_purchase_plan(
    migrated_settings: Settings,
    scenario: str,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    payload["input"]["scenario"] = scenario
    import json
    from pathlib import Path

    name = scenario.removeprefix("SYNTHETIC_").lower().replace("_", "-")
    family = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/synthetic/result-families" / f"{name}.json"
        ).read_text()
    )
    payload["expected_external_result"] = family["expected_external_result"]
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "BLOCKED" and plan.total_principal == 0
    assert plan.rows[0].candidate == source.members[0]
    assert plan.reasons == ("BUSINESS_PREREQUISITE_NOT_MET",)


def test_actual_buy_cost_replaces_the_prior_estimate_without_double_deduction(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, _ = feasible_payload(migrated_settings, expected_purchase_fees="100")
    command = payload["candidate_allocation"]
    command["policy"].update(entry_target_ratio="0.30", neighborhood_ratio="0.35")
    command["routes"][0]["minimum_commission"] = "2200"
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == 1900 and plan.remaining_cash == 0


def test_broker_final_cash_reserve_is_deducted_once(migrated_settings: Settings) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings, open_buy=True)
    command = payload["candidate_allocation"]
    command["commitments"] = [
        {
            "commitment_id": "synthetic-accepted-unfilled",
            "account_id": "synthetic-account-4017",
            "security_id": "SYNTH-CANDIDATE",
            "issuer_id": "fictional-new-issuer",
            "broker_order_id": "synthetic-open-buy",
            "principal": "300",
            "quantity": "30",
            "price_cap": "10",
            "purchase_cost": "20",
            "disposal_friction": "5",
            "evidence": deepcopy(command["securities"][0]["evidence"]),
        }
    ]
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == 400 and plan.remaining_cash == 3380
    assert plan.rows[0].candidate == source.members[0]


def test_legal_account_routes_minimize_full_cost_then_order_count(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, _ = feasible_payload(migrated_settings, account_cash=("4000", "4000"))
    command = payload["candidate_allocation"]
    first = command["routes"][0]
    first.update(
        minimum_commission="30",
        commission_ratio="0.001",
        minimum_quantity="3",
        quantity_increment="2",
    )
    second = deepcopy(first)
    second.update(
        account_id="synthetic-account-8029",
        minimum_commission="5",
        commission_ratio="0.002",
        minimum_quantity="7",
        quantity_increment="3",
    )
    command["routes"].append(second)
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    row = report.result.candidate_allocation.rows[0]
    # 7 + 21 * 3 = 70 is legal in the cheaper account. The alternative needs two orders.
    assert row.principal == 700 and len(row.legs) == 1
    assert row.legs[0].route.account_id == "synthetic-account-8029"
    assert row.legs[0].quantity == 70 and row.legs[0].purchase_cost == 5


def test_existing_security_cannot_be_relabelled_as_an_uncommitted_issuer(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings, security_id="XQZ-4017")
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    row = report.result.candidate_allocation.rows[0]
    assert row.principal == 0 and "ISSUER_IDENTITY_MISMATCH" in row.reasons
    assert row.candidate == source.members[0]


def test_foreign_candidate_report_is_blocked_without_disclosing_members(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, _ = feasible_payload(migrated_settings)
    payload["access_scope"]["user_id"] = "synthetic-unrelated-owner"
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "BLOCKED" and plan.rows == ()
    assert plan.reasons == ("CANDIDATE_HANDOFF_UNAVAILABLE",)


def test_superseded_risk_handoff_cannot_bypass_a_new_issuer_restriction(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload
    from test_issuer_concentration import concentration_payload
    from test_position_state_reconciliation import (
        position_evidence,
        refresh_current_position_evidence,
    )

    payload, source = feasible_payload(migrated_settings)
    newer = concentration_payload(
        migrated_settings,
        payload["candidate_allocation"]["risk_handoff"]["authorization_id"],
        quantity="100",
        identity="synthetic-newer-concentration",
    )
    cutoff = "2042-05-18T16:00:00Z"
    snapshot = newer["concentration"]["position_snapshot"]
    snapshot.update(cutoff_at=cutoff, snapshot_id="synthetic-newer-concentration-snapshot")
    refresh_current_position_evidence(snapshot, cutoff)
    newer["knowledge_cutoff"] = cutoff
    for account in snapshot["accounts"]:
        account["positions"][0]["market_price"] = "50"
        account["account_equity"] = str(int(account["cash_state"]["trading_cash"]) + 5000)
    for cost in newer["concentration"]["liquidation_costs"]:
        cost["evidence"] = position_evidence("synthetic-newer-cost", cutoff_at=cutoff)
    recent = run_frozen_decision_case(
        migrated_settings, newer, clock=GovernanceClock("2042-05-18T16:01:00Z")
    ).report
    assert recent is not None and recent.result.concentration is not None
    assert recent.result.concentration.disposition == "ASSESSED", recent.result.concentration
    assert recent.result.concentration.issuers[0].new_exposure_blocked
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "BLOCKED" and plan.total_principal == 0
    assert plan.rows[0].candidate == source.members[0]


@pytest.mark.parametrize("capital_state", ["CAUTION", "PRESERVATION"])
def test_non_normal_capital_state_cannot_create_new_candidate_exposure(
    migrated_settings: Settings, capital_state: str
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings, capital_state=capital_state)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-18T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "BLOCKED" and plan.total_principal == 0
    assert plan.rows[0].candidate == source.members[0]


@pytest.mark.parametrize("price", ["1E-400", "1E+100"])
def test_unrepresentable_price_basis_fails_closed_with_original_candidates(
    migrated_settings: Settings,
    price: str,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    route = payload["candidate_allocation"]["routes"][0]
    if price == "1E-400":
        route.update(price_cap=price, current_price=price, price_tick=price, minimum_price=price)
    else:
        route.update(price_cap=price, maximum_price=price, price_tick="0.01")
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "BLOCKED" and plan.rows[0].candidate == source.members[0]
    assert plan.reasons == ("ALLOCATION_OPTIMUM_UNAVAILABLE",)


def test_broker_reservation_must_cover_the_complete_committed_buy_cost(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings, open_buy=True)
    command = payload["candidate_allocation"]
    command["commitments"] = [
        {
            "commitment_id": "synthetic-broker-buy-cost",
            "account_id": "synthetic-account-4017",
            "security_id": "SYNTH-CANDIDATE",
            "issuer_id": "fictional-new-issuer",
            "broker_order_id": "synthetic-open-buy",
            "principal": "300",
            "quantity": "30",
            "price_cap": "10",
            "purchase_cost": "4000",
            "disposal_friction": "0",
            "evidence": deepcopy(command["securities"][0]["evidence"]),
        }
    ]
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "BLOCKED" and plan.rows[0].candidate == source.members[0]
    assert plan.reasons == ("BUY_COMMITMENT_RESERVATION_INCOMPLETE",)


def test_unused_small_unit_route_cannot_reduce_the_selected_route_residual_allowance(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings, candidate_count=2)
    command = payload["candidate_allocation"]
    command["policy"]["neighborhood_ratio"] = "0.012"
    command["commitments"] = [
        {
            "commitment_id": "synthetic-small-existing-buy",
            "account_id": "synthetic-account-4017",
            "security_id": "SYNTH-CANDIDATE",
            "issuer_id": "fictional-new-issuer",
            "broker_order_id": None,
            "principal": "20",
            "quantity": "2",
            "price_cap": "10",
            "purchase_cost": "0",
            "disposal_friction": "0",
            "evidence": deepcopy(command["securities"][0]["evidence"]),
        }
    ]
    funded, peer = command["routes"]
    funded.update(minimum_quantity="6", quantity_increment="6")
    peer.update(minimum_quantity="0.1", quantity_increment="0.1")
    unused = deepcopy(funded)
    unused.update(
        account_id="synthetic-account-8029", minimum_quantity="0.1", quantity_increment="0.1"
    )
    command["routes"].append(unused)
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert [row.continuous_principal for row in plan.rows] == [40, 60]
    assert [row.principal for row in plan.rows] == [60, 40]
    assert tuple(row.candidate for row in plan.rows) == source.members


def test_complete_discrete_tie_uses_security_identity_instead_of_source_order(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(
        migrated_settings, candidate_count=2, security_id="Z-SYNTH-CANDIDATE"
    )
    command = payload["candidate_allocation"]
    command["policy"]["neighborhood_ratio"] = "0.01"
    for route in command["routes"]:
        route.update(minimum_quantity="6", quantity_increment="6")
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert [row.continuous_principal for row in plan.rows] == [50, 50]
    assert [row.principal for row in plan.rows] == [0, 60]
    assert tuple(row.candidate for row in plan.rows) == source.members


@pytest.mark.parametrize("change", ["expired", "missing", "REVOKED", "SUSPENDED", "restored"])
def test_current_qualification_failure_cannot_revive_or_allocate_a_saved_candidate(
    migrated_settings: Settings, change: str
) -> None:
    from datetime import datetime

    from synthetic_candidate_allocation import feasible_payload, save_qualification_prior

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger

    payload, source = feasible_payload(
        migrated_settings,
        qualification_valid_through="2042-05-17T15:30:00Z"
        if change == "expired"
        else "2042-05-22T15:00:00Z",
        seed_qualification=change != "missing",
    )
    ledger = DecisionLedger.from_settings(migrated_settings)
    if change in {"REVOKED", "SUSPENDED", "restored"}:
        with ledger.serialize_case_execution() as connection:
            fact = ledger.get_decision_event(
                payload["candidate_allocation"]["candidate_event_id"], connection
            )
            assert fact is not None and fact.case.access_scope is not None
            history = ledger.governance_history(connection, fact.case.access_scope)
        previous = history[-1].qualification
        assert previous is not None
        revoked = previous.model_copy(
            update={
                "decision_id": "synthetic-allocation-qualification-withdrawal",
                "previous_decision_id": previous.decision_id,
                "status": "SUSPENDED" if change == "SUSPENDED" else "REVOKED",
                "cause": "AUTHORIZATION_REVOKED",
                "recorded_at": datetime.fromisoformat("2042-05-17T15:20:00Z"),
            }
        )
        save_qualification_prior(migrated_settings, fact.case, revoked)
        if change == "restored":
            restored = previous.model_copy(
                update={
                    "decision_id": "synthetic-allocation-qualification-restoration",
                    "previous_decision_id": revoked.decision_id,
                    "recorded_at": datetime.fromisoformat("2042-05-17T15:40:00Z"),
                }
            )
            save_qualification_prior(migrated_settings, fact.case, restored)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "BLOCKED" and plan.total_principal == 0
    assert plan.rows[0].candidate == source.members[0]


def test_corrected_candidate_conclusion_cannot_allocate_using_its_original_report(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.bootstrap.decision_cases import correct_default_frozen_decision_case

    payload, source = feasible_payload(migrated_settings)
    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        original = ledger.get_decision_event(
            payload["candidate_allocation"]["candidate_event_id"], connection
        )
        assert original is not None
    correction = correct_default_frozen_decision_case(
        migrated_settings,
        original.case.business_identity,
        clock=GovernanceClock("2042-05-17T16:00:30Z"),
    )
    assert correction.report is not None
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "BLOCKED" and plan.rows[0].candidate == source.members[0]
    assert plan.reasons == ("CANDIDATE_CONCLUSION_SUPERSEDED",)


def test_continuous_fractional_equalization_also_respects_exact_stress_capacity(
    migrated_settings: Settings,
) -> None:
    from decimal import Decimal

    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings, candidate_count=3)
    payload["candidate_allocation"]["policy"].update(
        entry_target_ratio="0.30", neighborhood_ratio="0.50"
    )
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "PLANNED"
    assert tuple(row.candidate for row in plan.rows) == source.members
    assert sum(row.continuous_principal for row in plan.rows) * Decimal("0.23") <= 940
    assert len({row.continuous_principal for row in plan.rows}) == 1


@pytest.mark.parametrize("neighborhood_ratio,upper_count", [("0.24", 4), ("0.216", 1)])
def test_joint_completion_ratios_cover_a_ten_candidate_cohort_without_permutation_search(
    migrated_settings: Settings,
    neighborhood_ratio: str,
    upper_count: int,
) -> None:
    from decimal import Decimal

    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings, candidate_count=10)
    payload["candidate_allocation"]["policy"]["neighborhood_ratio"] = neighborhood_ratio
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "PLANNED", plan.reasons
    assert tuple(row.candidate for row in plan.rows) == source.members
    assert {row.continuous_principal for row in plan.rows} == {Decimal(neighborhood_ratio) * 1000}
    assert (
        sorted(row.principal for row in plan.rows)
        == [200] * (10 - upper_count) + [300] * upper_count
    )
    assert plan.total_principal == 2000 + upper_count * 100


def test_uncovered_candidate_retains_each_discrete_priority_comparison(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(
        migrated_settings, candidate_count=2, security_id="Z-SYNTH-CANDIDATE"
    )
    command = payload["candidate_allocation"]
    command["policy"]["neighborhood_ratio"] = "0.01"
    for route in command["routes"]:
        route.update(minimum_quantity="6", quantity_increment="6")
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    row = plan.rows[0]
    assert row.candidate == source.members[0] and row.principal == 0
    assert "UNALLOCATED_CAPACITY_PRIORITY" in row.reasons
    assert [entry.criterion for entry in row.comparisons] == [
        "COVERAGE",
        "COMPLETION_RATIOS",
        "PRINCIPAL",
        "PROBABILITIES",
        "PURCHASE_COST",
        "ORDER_COUNT",
        "STABLE_IDENTITIES",
    ]
    assert [entry.relation for entry in row.comparisons] == ["EQUAL"] * 6 + ["WORSE"]
    assert row.comparisons[0].selected_value == (Decimal(1),)
    assert row.comparisons[1].selected_value == row.comparisons[1].alternative_value
    assert row.comparisons[2].selected_value == (Decimal(60),)
    assert row.comparisons[6].selected_value == (Decimal(60), Decimal(0))
    assert row.comparisons[6].alternative_value == (Decimal(0), Decimal(60))
    assert plan.discrete_objectives == tuple(entry.selected_value for entry in row.comparisons)


def test_below_minimum_unit_has_a_distinct_reason_and_infeasible_comparison(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    command["routes"][0].update(minimum_quantity="100", quantity_increment="100")
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    row = report.result.candidate_allocation.rows[0]
    assert row.candidate == source.members[0] and row.continuous_principal == 700
    assert row.principal == 0 and row.primary_reason == "BELOW_MINIMUM_BUY_UNIT"
    assert "UNALLOCATED_CAPACITY_PRIORITY" not in row.reasons
    assert len(row.comparisons) == 7
    assert all(entry.relation == "INFEASIBLE" for entry in row.comparisons)


def test_route_failures_keep_all_reasons_and_fixed_primary_precedence(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    route = command["routes"][0]
    route.update(permission=False, current_price=None, rule_version="unknown-rule")
    route["evidence"]["expires_at"] = "2042-05-17T15:30:00Z"
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    row = report.result.candidate_allocation.rows[0]
    assert row.candidate == source.members[0] and row.principal == 0
    assert row.primary_reason == "ACCOUNT_PERMISSION_DENIED"
    assert {
        "ACCOUNT_PERMISSION_DENIED",
        "BUY_ROUTE_EVIDENCE_FAILED",
        "CURRENT_PRICE_UNAVAILABLE",
    }.issubset(row.reasons)
    assert len(row.route_failures) == 1
    assert row.route_failures[0].route.account_id == route["account_id"]
    assert set(row.route_failures[0].reasons).issuperset(row.reasons)


def test_unaffordable_minimum_commission_explains_zero_continuous_capacity(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_allocation import feasible_payload

    payload, source = feasible_payload(migrated_settings)
    command = payload["candidate_allocation"]
    command["routes"][0]["minimum_commission"] = "5000"
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    row = report.result.candidate_allocation.rows[0]
    assert row.candidate == source.members[0] and row.continuous_principal == 0
    assert row.principal == 0 and row.primary_reason == "CASH_CAPACITY_EXHAUSTED"
