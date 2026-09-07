from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from typing import Any

import pytest

from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    FrozenDecisionCase,
    load_frozen_decision_case,
)
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.position_management.contracts import PositionSnapshotCommand
from stock_profiler.modules.position_management.service import reconcile


def position_case_payload(
    settings: Settings,
    identity: str,
    command: dict[str, Any],
) -> dict[str, Any]:
    payload = load_frozen_decision_case(settings).model_dump(mode="json")
    account_ids = [account["account_id"] for account in command["accounts"]]
    payload["business_identity"] = f"synthetic:position-state:{identity}"
    payload["case_id"] = f"d0-position-state-{identity}"
    payload["version_bundle"].update(
        case_contract_version="7.0.0",
        host_contract_version="7.0.0",
        report_projection_contract_version="7.0.0",
        agent_definition_version="2.0.0",
    )
    payload["agent_definition"]["version"] = "2.0.0"
    payload["access_scope"] = {
        "contract_version": "1.0.0",
        "user_id": "stock-profiler-single-user",
        "account_ids": account_ids,
        "visibility": "USER",
    }
    payload["input"]["account"]["account_id"] = account_ids[0]
    payload["knowledge_cutoff"] = command["cutoff_at"]
    payload["position"] = command
    return payload


def position_evidence(
    source: str,
    *,
    cutoff_at: str = "2042-05-17T16:00:00Z",
) -> dict[str, str]:
    day = cutoff_at.partition("T")[0]
    return {
        "source": source,
        "source_version": "synthetic-broker-schema-v1",
        "business_effective_at": f"{day}T15:00:00Z",
        "source_observed_at": f"{day}T15:10:00Z",
        "locally_acquired_at": f"{day}T15:14:00Z",
        "validated_at": f"{day}T15:18:00Z",
        "cutoff_at": cutoff_at,
        "complete_through_at": cutoff_at,
    }


def refresh_current_position_evidence(command: dict[str, Any], cutoff_at: str) -> None:
    evidences = [command["snapshot_evidence"]]
    for account in command["accounts"]:
        cash = account["cash_state"]
        evidences.extend(
            [
                account["snapshot_evidence"],
                account["account_equity_evidence"],
                cash["opening_ledger_cash_evidence"],
                cash["evidence"],
            ]
        )
        evidences.extend(position["evidence"] for position in account["positions"])
        evidences.extend(order["evidence"] for order in account["open_orders"])
        evidences.extend(
            restriction["evidence"] for restriction in account["execution_restrictions"]
        )
    for evidence in evidences:
        evidence.update(position_evidence(evidence["source"], cutoff_at=cutoff_at))


def position_snapshot_command() -> dict[str, Any]:
    return {
        "operation": "POSITION_SNAPSHOT_RECONCILE",
        "contract_version": "1.0.0",
        "synthetic": True,
        "generator_version": "position-state-fixture-v1",
        "seed": 6206,
        "snapshot_id": "synthetic-position-snapshot-alpha",
        "cutoff_at": "2042-05-17T16:00:00Z",
        "snapshot_evidence": position_evidence("synthetic-position-snapshot-manifest"),
        "valuation_currency": "XSP",
        "accounts": [
            {
                "account_id": "synthetic-account-4017",
                "account_type": "SIMULATED_CASH",
                "currency": "XSP",
                "snapshot_evidence": position_evidence("synthetic-broker-4017-snapshot"),
                "account_equity": "1310",
                "account_equity_evidence": position_evidence("synthetic-broker-4017-equity"),
                "cash_state": {
                    "cash_availability_semantics": "BROKER_FINAL_CASH_LAYERS",
                    "ledger_cash_semantics": "OPENING_BALANCE_PLUS_AUTHORITATIVE_LEDGER",
                    "opening_ledger_cash": "902",
                    "opening_ledger_cash_evidence": position_evidence(
                        "synthetic-broker-4017-opening-cash"
                    ),
                    "ledger_cash": "100",
                    "trading_cash": "95",
                    "transferable_cash": "90",
                    "frozen_cash": "5",
                    "receivable_cash": "10",
                    "payable_cash": "0",
                    "evidence": position_evidence("synthetic-broker-4017-cash"),
                },
                "positions": [
                    {
                        "position_id": "synthetic-position-4017-xqz",
                        "origin": "EXTERNAL",
                        "lifecycle_id": "synthetic-lifecycle-4017-xqz",
                        "issuer_id": "FICTIONAL-ORBITAL-MOSAIC",
                        "security_id": "XQZ-4017",
                        "total_quantity": "100",
                        "broker_sellable_quantity": "70",
                        "sellable_quantity_semantics": "BROKER_FINAL_SELLABLE",
                        "encumbrance_quantity_semantics": "OVERLAPPING_NON_SELLABLE",
                        "unsettled_quantity": "10",
                        "frozen_quantity": "5",
                        "restricted_quantity": "0",
                        "open_sell_order_quantity": "15",
                        "reported_cost_basis": "900",
                        "market_price": "12",
                        "evidence": position_evidence("synthetic-broker-4017-position"),
                    }
                ],
                "open_orders": [
                    {
                        "order_id": "synthetic-open-sell-4017",
                        "security_id": "XQZ-4017",
                        "side": "SELL",
                        "remaining_quantity": "15",
                        "reserved_cash": None,
                        "reserved_cash_semantics": "NOT_APPLICABLE",
                        "evidence": position_evidence("synthetic-broker-4017-order"),
                    }
                ],
                "ledger_entries": [
                    {
                        "entry_id": "synthetic-fill-4017-xqz",
                        "entry_type": "FILL",
                        "security_id": "XQZ-4017",
                        "quantity_delta": "100",
                        "cost_basis_delta": "890",
                        "cash_delta": "-890",
                        "occurred_at": "2042-05-16T15:00:00Z",
                        "evidence": position_evidence("synthetic-broker-4017-fill"),
                    },
                    {
                        "entry_id": "synthetic-fee-4017-xqz",
                        "entry_type": "FEE",
                        "security_id": "XQZ-4017",
                        "quantity_delta": "0",
                        "cost_basis_delta": "10",
                        "cash_delta": "-10",
                        "occurred_at": "2042-05-16T15:00:01Z",
                        "evidence": position_evidence("synthetic-broker-4017-fee"),
                    },
                    {
                        "entry_id": "synthetic-transfer-4017-cash",
                        "entry_type": "TRANSFER_IN",
                        "security_id": None,
                        "quantity_delta": "0",
                        "cost_basis_delta": "0",
                        "cash_delta": "100",
                        "occurred_at": "2042-05-16T15:00:02Z",
                        "evidence": position_evidence("synthetic-broker-4017-transfer"),
                    },
                    {
                        "entry_id": "synthetic-transfer-out-4017-cash",
                        "entry_type": "TRANSFER_OUT",
                        "security_id": None,
                        "quantity_delta": "0",
                        "cost_basis_delta": "0",
                        "cash_delta": "-2",
                        "occurred_at": "2042-05-16T15:00:03Z",
                        "evidence": position_evidence("synthetic-broker-4017-transfer-out"),
                    },
                ],
                "execution_restrictions": [],
            },
            {
                "account_id": "synthetic-account-8029",
                "account_type": "SIMULATED_CASH",
                "currency": "XSP",
                "snapshot_evidence": position_evidence("synthetic-broker-8029-snapshot"),
                "account_equity": "800",
                "account_equity_evidence": position_evidence("synthetic-broker-8029-equity"),
                "cash_state": {
                    "cash_availability_semantics": "BROKER_FINAL_CASH_LAYERS",
                    "ledger_cash_semantics": "OPENING_BALANCE_PLUS_AUTHORITATIVE_LEDGER",
                    "opening_ledger_cash": "650",
                    "opening_ledger_cash_evidence": position_evidence(
                        "synthetic-broker-8029-opening-cash"
                    ),
                    "ledger_cash": "200",
                    "trading_cash": "200",
                    "transferable_cash": "200",
                    "frozen_cash": "0",
                    "receivable_cash": "0",
                    "payable_cash": "0",
                    "evidence": position_evidence("synthetic-broker-8029-cash"),
                },
                "positions": [
                    {
                        "position_id": "synthetic-position-8029-xqz",
                        "origin": "EXTERNAL",
                        "lifecycle_id": "synthetic-lifecycle-8029-xqz",
                        "issuer_id": "FICTIONAL-ORBITAL-MOSAIC",
                        "security_id": "XQZ-4017",
                        "total_quantity": "50",
                        "broker_sellable_quantity": "45",
                        "sellable_quantity_semantics": "BROKER_FINAL_SELLABLE",
                        "encumbrance_quantity_semantics": "OVERLAPPING_NON_SELLABLE",
                        "unsettled_quantity": "5",
                        "frozen_quantity": "0",
                        "restricted_quantity": "0",
                        "open_sell_order_quantity": "0",
                        "reported_cost_basis": "450",
                        "market_price": "12",
                        "evidence": position_evidence("synthetic-broker-8029-position"),
                    }
                ],
                "open_orders": [],
                "ledger_entries": [
                    {
                        "entry_id": "synthetic-fill-8029-xqz",
                        "entry_type": "FILL",
                        "security_id": "XQZ-4017",
                        "quantity_delta": "50",
                        "cost_basis_delta": "450",
                        "cash_delta": "-450",
                        "occurred_at": "2042-05-16T15:00:00Z",
                        "evidence": position_evidence("synthetic-broker-8029-fill"),
                    },
                    {
                        "entry_id": "synthetic-corporate-action-8029-xqz",
                        "entry_type": "CORPORATE_ACTION",
                        "security_id": "XQZ-4017",
                        "quantity_delta": "0",
                        "cost_basis_delta": "0",
                        "cash_delta": "0",
                        "occurred_at": "2042-05-16T15:00:01Z",
                        "evidence": position_evidence("synthetic-broker-8029-corporate-action"),
                    },
                ],
                "execution_restrictions": [],
            },
        ],
        "annotations": [
            {
                "annotation_id": "synthetic-user-position-note-4017",
                "account_id": "synthetic-account-4017",
                "security_id": "XQZ-4017",
                "note": "Synthetic user note retains a different claimed quantity.",
                "claimed_total_quantity": "999",
                "created_at": "2042-05-17T15:30:00Z",
            }
        ],
    }


def test_position_snapshot_reconciles_authoritative_account_facts_and_replays_exactly(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    payload = position_case_payload(migrated_settings, "reconciled", deepcopy(command))

    first = run_frozen_decision_case(migrated_settings, payload)

    assert first.report is not None
    outcome = first.report.result.position
    assert outcome is not None
    assert outcome.disposition == "RECONCILED"
    assert outcome.reasons == ("AUTHORITATIVE_POSITION_FACTS_RECONCILED",)
    assert outcome.snapshot.snapshot_id == "synthetic-position-snapshot-alpha"
    assert outcome.snapshot.cutoff_at.isoformat() == "2042-05-17T16:00:00+00:00"
    evidence_clock = outcome.snapshot.evidence_clock
    assert evidence_clock.business_effective_at is not None
    assert evidence_clock.source_observed_at is not None
    assert evidence_clock.locally_acquired_at is not None
    assert evidence_clock.validated_at is not None
    assert evidence_clock.business_effective_at.isoformat() == ("2042-05-17T15:00:00+00:00")
    assert evidence_clock.source_observed_at.isoformat() == ("2042-05-17T15:10:00+00:00")
    assert evidence_clock.locally_acquired_at.isoformat() == ("2042-05-17T15:14:00+00:00")
    assert evidence_clock.validated_at.isoformat() == "2042-05-17T15:18:00+00:00"
    assert outcome.snapshot.total_account_equity == Decimal("2110")
    assert len(outcome.snapshot.action_units) == 2
    assert [unit.account_id for unit in outcome.snapshot.action_units] == [
        "synthetic-account-4017",
        "synthetic-account-8029",
    ]
    assert outcome.snapshot.action_units[0].total_quantity == Decimal("100")
    assert outcome.snapshot.action_units[0].position_id == "synthetic-position-4017-xqz"
    assert outcome.snapshot.action_units[0].origin == "EXTERNAL"
    assert outcome.snapshot.action_units[0].lifecycle_id == "synthetic-lifecycle-4017-xqz"
    assert outcome.snapshot.action_units[0].position_evidence.source == (
        "synthetic-broker-4017-position"
    )
    assert outcome.snapshot.action_units[0].position_evidence.source_version == (
        "synthetic-broker-schema-v1"
    )
    assert outcome.snapshot.action_units[0].exact_statistical_action_quantity == Decimal("70")
    assert outcome.snapshot.action_units[0].exact_quantity_status == "AVAILABLE"
    assert outcome.snapshot.action_units[1].total_quantity == Decimal("50")
    assert outcome.snapshot.action_units[1].exact_statistical_action_quantity == Decimal("45")
    assert outcome.snapshot.issuer_exposures[0].issuer_id == "FICTIONAL-ORBITAL-MOSAIC"
    assert outcome.snapshot.issuer_exposures[0].current_market_exposure == Decimal("1800")
    assert outcome.snapshot.issuer_exposures[0].account_ids == (
        "synthetic-account-4017",
        "synthetic-account-8029",
    )
    assert outcome.snapshot.cash_states[0].transferable_cash == Decimal("90")
    assert outcome.snapshot.cash_states[0].opening_ledger_cash == Decimal("902")
    assert outcome.snapshot.cash_states[0].account_evidence.source == (
        "synthetic-broker-4017-snapshot"
    )
    assert outcome.snapshot.cash_states[0].account_equity_evidence.source == (
        "synthetic-broker-4017-equity"
    )
    assert outcome.snapshot.cash_states[0].cash_state_evidence.source == (
        "synthetic-broker-4017-cash"
    )
    assert [entry.entry_type for entry in outcome.snapshot.authoritative_ledger] == [
        "FILL",
        "FEE",
        "TRANSFER_IN",
        "TRANSFER_OUT",
        "FILL",
        "CORPORATE_ACTION",
    ]
    assert outcome.snapshot.action_units[0].total_quantity != Decimal("999")
    assert outcome.snapshot.user_annotations[0].claimed_total_quantity == Decimal("999")
    assert outcome.conflicts == ()
    position_stage = next(
        stage for stage in first.report.stage_results if stage.phase == "POSITION_RECONCILIATION"
    )
    assert position_stage.status == "SUCCEEDED"
    assert position_stage.gate_results[0].gate_id == "AUTHORITATIVE_POSITION_FACTS"
    assert (
        get_formal_report(
            first.report_version_id,
            migrated_settings,
            principal=AccessPrincipal(
                user_id="stock-profiler-single-user",
                account_ids=("synthetic-account-4017", "synthetic-account-8029"),
                permissions=("REPORT_READ",),
            ),
        )
        == first.report
    )

    replay = run_frozen_decision_case(migrated_settings, payload)

    assert replay.report == first.report


def test_position_snapshot_retains_conflicts_and_blocks_only_affected_exact_quantities(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    position = account["positions"][0]
    position["total_quantity"] = "101"
    position["reported_cost_basis"] = "901"
    position["broker_sellable_quantity"] = None
    position["sellable_quantity_semantics"] = "UNKNOWN"
    position["unsettled_quantity"] = None
    position["frozen_quantity"] = None
    position["restricted_quantity"] = None
    position["market_price"] = None
    position["evidence"]["source"] = None
    position["evidence"]["cutoff_at"] = None
    position["evidence"]["expires_at"] = "2042-05-17T15:59:59Z"
    account["open_orders"][0]["remaining_quantity"] = "14"
    account["account_equity"] = None
    payload = position_case_payload(migrated_settings, "conflicted", command)

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is not None
    outcome = execution.report.result.position
    assert outcome is not None
    assert outcome.disposition == "CONFLICTED"
    assert outcome.snapshot.action_units[0].total_quantity == Decimal("101")
    assert outcome.snapshot.action_units[0].exact_statistical_action_quantity is None
    assert outcome.snapshot.action_units[0].exact_quantity_status == "BLOCKED"
    assert outcome.snapshot.action_units[1].exact_statistical_action_quantity is None
    assert outcome.snapshot.action_units[1].exact_quantity_status == "BLOCKED"
    assert outcome.snapshot.total_account_equity is None
    assert {
        conflict.code
        for conflict in outcome.conflicts
        if conflict.affected_scope.account_id == "synthetic-account-4017"
    } >= {
        "ACCOUNT_EQUITY_MISSING",
        "SOURCE_MISSING",
        "FACT_CUTOFF_MISSING",
        "FACT_EXPIRED",
        "BROKER_SELLABLE_QUANTITY_MISSING",
        "SELLABLE_QUANTITY_SEMANTICS_UNKNOWN",
        "UNSETTLED_QUANTITY_MISSING",
        "FROZEN_QUANTITY_MISSING",
        "RESTRICTED_QUANTITY_MISSING",
        "MARKET_PRICE_MISSING",
        "LEDGER_QUANTITY_MISMATCH",
        "LEDGER_COST_BASIS_MISMATCH",
        "OPEN_SELL_ORDER_MISMATCH",
    }
    assert any(
        conflict.affected_scope.account_id == "synthetic-account-4017"
        and conflict.code == "ACCOUNT_EQUITY_MISSING"
        for conflict in outcome.conflicts
    )
    assert any(
        conflict.blocks_exact_statistical_quantity
        and conflict.affected_scope.security_id == "XQZ-4017"
        for conflict in outcome.conflicts
    )
    position_stage = next(
        stage
        for stage in execution.report.stage_results
        if stage.phase == "POSITION_RECONCILIATION"
    )
    assert position_stage.status == "REJECTED"
    assert position_stage.gate_results[0].status == "FAILED"

    replay = run_frozen_decision_case(migrated_settings, payload)

    assert replay.report == execution.report


def test_position_snapshot_requires_its_frozen_scope_and_knowledge_cutoff(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    scope_mismatch = position_case_payload(
        migrated_settings,
        "scope-mismatch",
        deepcopy(command),
    )
    scope_mismatch["access_scope"]["account_ids"] = ["synthetic-account-4017"]

    with pytest.raises(ValueError, match="position snapshot and frozen access scope must agree"):
        FrozenDecisionCase.model_validate(scope_mismatch)

    cutoff_mismatch = position_case_payload(
        migrated_settings,
        "cutoff-mismatch",
        deepcopy(command),
    )
    cutoff_mismatch["knowledge_cutoff"] = "2042-05-17T16:01:00Z"

    with pytest.raises(ValueError, match="position snapshot and frozen access scope must agree"):
        FrozenDecisionCase.model_validate(cutoff_mismatch)


def test_position_snapshot_rejects_non_cash_account_before_creating_a_run(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["account_type"] = "SIMULATED_MARGIN"
    payload = position_case_payload(migrated_settings, "non-cash", command)

    with pytest.raises(ValueError, match="SIMULATED_CASH"):
        FrozenDecisionCase.model_validate(payload)


def test_position_snapshot_blocks_account_wide_restrictions_and_post_cutoff_facts(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    position = account["positions"][0]
    position["broker_sellable_quantity"] = "101"
    position["market_price"] = None
    account["cash_state"]["evidence"]["expires_at"] = "2042-05-17T15:59:59Z"
    account["ledger_entries"][0]["occurred_at"] = "2042-05-18T15:00:00Z"
    account["execution_restrictions"] = [
        {
            "restriction_id": "synthetic-account-wide-sell-block",
            "security_id": None,
            "kind": "BROKER_SELL_BLOCK",
            "reason": "Synthetic account-wide broker restriction.",
            "active": True,
            "evidence": position_evidence("synthetic-broker-4017-restriction"),
        }
    ]
    command["annotations"][0]["created_at"] = "2042-05-18T15:30:00Z"
    payload = position_case_payload(migrated_settings, "critical-facts", command)

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is not None
    outcome = execution.report.result.position
    assert outcome is not None
    assert outcome.disposition == "CONFLICTED"
    assert outcome.snapshot.action_units[0].exact_statistical_action_quantity is None
    assert outcome.snapshot.action_units[0].exact_quantity_status == "BLOCKED"
    assert outcome.snapshot.action_units[1].exact_statistical_action_quantity is None
    assert outcome.snapshot.cash_states[0].account_equity == Decimal("1310")
    assert outcome.snapshot.unfinished_orders[0].order_id == "synthetic-open-sell-4017"
    assert outcome.snapshot.execution_restrictions[0].restriction_id == (
        "synthetic-account-wide-sell-block"
    )
    assert {
        conflict.code
        for conflict in outcome.conflicts
        if conflict.affected_scope.account_id == "synthetic-account-4017"
    } >= {
        "BROKER_SELLABLE_QUANTITY_OUT_OF_BOUNDS",
        "MARKET_PRICE_MISSING",
        "FACT_EXPIRED",
        "LEDGER_ENTRY_AFTER_CUTOFF",
        "ANNOTATION_AFTER_CUTOFF",
    }


def test_position_snapshot_never_publishes_partial_issuer_exposure(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["positions"] = []
    payload = position_case_payload(migrated_settings, "summary-missing", command)

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is not None
    outcome = execution.report.result.position
    assert outcome is not None
    assert outcome.disposition == "CONFLICTED"
    assert "BROKER_POSITION_SUMMARY_MISSING" in {conflict.code for conflict in outcome.conflicts}
    assert outcome.snapshot.issuer_exposures[0].issuer_id == "FICTIONAL-ORBITAL-MOSAIC"
    assert outcome.snapshot.issuer_exposures[0].current_market_exposure is None


def test_position_snapshot_blocks_all_quantities_for_unreconciled_cash_and_equity(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    account["cash_state"]["ledger_cash"] = "99"
    account["account_equity"] = "1308"
    account["open_orders"].append(
        {
            "order_id": "synthetic-open-buy-unresolved",
            "security_id": "NEW-4017",
            "side": "BUY",
            "remaining_quantity": None,
            "reserved_cash": None,
            "reserved_cash_semantics": "BROKER_FINAL_RESERVED_CASH",
            "evidence": position_evidence("synthetic-broker-4017-buy"),
        }
    )
    payload = position_case_payload(migrated_settings, "cash-equity-conflict", command)

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is not None
    outcome = execution.report.result.position
    assert outcome is not None
    assert outcome.disposition == "CONFLICTED"
    assert all(
        unit.exact_statistical_action_quantity is None and unit.exact_quantity_status == "BLOCKED"
        for unit in outcome.snapshot.action_units
    )
    assert {conflict.code for conflict in outcome.conflicts} >= {
        "LEDGER_CASH_MISMATCH",
        "ACCOUNT_EQUITY_MISMATCH",
        "BUY_ORDER_REMAINING_QUANTITY_MISSING",
        "BUY_ORDER_RESERVED_CASH_MISSING",
    }


def test_position_snapshot_rejects_impossible_quantities_and_negative_valuations(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    position = command["accounts"][0]["positions"][0]
    position["frozen_quantity"] = "101"
    position["restricted_quantity"] = "-1"
    position["market_price"] = "-12"
    payload = position_case_payload(migrated_settings, "invalid-quantities", command)

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is not None
    outcome = execution.report.result.position
    assert outcome is not None
    assert outcome.disposition == "CONFLICTED"
    assert outcome.snapshot.action_units[0].exact_statistical_action_quantity is None
    assert outcome.snapshot.issuer_exposures[0].current_market_exposure is None
    assert {
        conflict.code
        for conflict in outcome.conflicts
        if conflict.affected_scope.account_id == "synthetic-account-4017"
    } >= {
        "FROZEN_QUANTITY_OUT_OF_BOUNDS",
        "RESTRICTED_QUANTITY_NEGATIVE",
        "MARKET_PRICE_NON_POSITIVE",
    }


def test_position_snapshot_rejects_conflicting_security_issuer_identity(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][1]["positions"][0]["issuer_id"] = "FICTIONAL-CONFLICTING-ISSUER"
    payload = position_case_payload(migrated_settings, "issuer-identity-conflict", command)

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is not None
    outcome = execution.report.result.position
    assert outcome is not None
    assert outcome.disposition == "CONFLICTED"
    assert {
        exposure.issuer_id: exposure.current_market_exposure
        for exposure in outcome.snapshot.issuer_exposures
    } == {
        "FICTIONAL-CONFLICTING-ISSUER": None,
        "FICTIONAL-ORBITAL-MOSAIC": None,
    }
    assert all(
        unit.exact_statistical_action_quantity is None
        and "SECURITY_ISSUER_IDENTITY_CONFLICT" in unit.reasons
        for unit in outcome.snapshot.action_units
    )


def test_position_snapshot_keeps_valuation_when_execution_is_restricted(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["execution_restrictions"] = [
        {
            "restriction_id": "synthetic-sell-block",
            "security_id": "XQZ-4017",
            "kind": "BROKER_SELL_BLOCK",
            "reason": "Synthetic broker sell restriction.",
            "active": True,
            "evidence": position_evidence("synthetic-broker-4017-restriction"),
        }
    ]
    payload = position_case_payload(migrated_settings, "restricted-exposure", command)

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is not None
    outcome = execution.report.result.position
    assert outcome is not None
    assert outcome.disposition == "RECONCILED"
    assert outcome.snapshot.action_units[0].exact_statistical_action_quantity is None
    assert outcome.snapshot.action_units[1].exact_statistical_action_quantity == Decimal("45")
    assert outcome.snapshot.issuer_exposures[0].current_market_exposure == Decimal("1800")


def test_position_snapshot_requires_current_completeness_watermarks(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["positions"][0]["evidence"]["complete_through_at"] = (
        "2042-05-17T15:59:59Z"
    )
    payload = position_case_payload(migrated_settings, "stale-position-evidence", command)

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is not None
    outcome = execution.report.result.position
    assert outcome is not None
    assert outcome.disposition == "CONFLICTED"
    assert outcome.snapshot.action_units[0].exact_statistical_action_quantity is None
    assert "FACT_COMPLETENESS_WATERMARK_INSUFFICIENT" in {
        conflict.code for conflict in outcome.conflicts
    }


def test_position_snapshot_conflicts_have_unique_deterministic_identifiers(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    account["snapshot_evidence"]["source"] = None
    account["account_equity_evidence"]["source"] = None
    account["cash_state"]["evidence"]["source"] = None
    payload = position_case_payload(migrated_settings, "unique-conflicts", command)

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.report is not None
    outcome = execution.report.result.position
    assert outcome is not None
    conflict_ids = tuple(conflict.conflict_id for conflict in outcome.conflicts)
    assert len(conflict_ids) == len(set(conflict_ids))


def test_position_snapshot_reconciles_identically_in_fresh_processes() -> None:
    command = position_snapshot_command()
    command["accounts"][0]["positions"] = []
    for security_id in ("AAA-4017", "BBB-4017", "CCC-4017"):
        command["accounts"][0]["ledger_entries"].append(
            {
                "entry_id": f"synthetic-orphan-{security_id}",
                "entry_type": "CORPORATE_ACTION",
                "security_id": security_id,
                "quantity_delta": "0",
                "cost_basis_delta": "0",
                "cash_delta": "0",
                "occurred_at": "2042-05-16T15:00:04Z",
                "evidence": position_evidence(f"synthetic-broker-{security_id}"),
            }
        )

    script = """
import json
import os
from stock_profiler.modules.position_management.contracts import PositionSnapshotCommand
from stock_profiler.modules.position_management.service import reconcile

command = PositionSnapshotCommand.model_validate(json.loads(os.environ["POSITION_COMMAND"]))
print(reconcile(command).model_dump_json())
"""
    outputs = []
    for hash_seed in ("1", "2", "3"):
        environment = os.environ | {
            "PYTHONHASHSEED": hash_seed,
            "POSITION_COMMAND": json.dumps(command),
        }
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            cwd=Path(__file__).parents[2],
            env=environment,
            text=True,
        )
        outputs.append(completed.stdout)

    assert outputs[0] == outputs[1] == outputs[2]


def test_position_snapshot_uses_its_fixed_decimal_contract() -> None:
    payload = position_snapshot_command()
    position = payload["accounts"][0]["positions"][0]
    position["total_quantity"] = "1.234567890123456789012345678901234"
    position["market_price"] = "1.234567890123456789012345678901234"
    payload["accounts"][0]["ledger_entries"][0]["quantity_delta"] = position["total_quantity"]
    command = PositionSnapshotCommand.model_validate(payload)

    baseline = reconcile(command)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = False
        low_precision = reconcile(command)

    assert low_precision == baseline


def test_position_snapshot_rejects_mutated_historical_ledger_entry(
    migrated_settings: Settings,
) -> None:
    original = position_snapshot_command()
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "historical-ledger-original", original),
    )
    assert first.report is not None

    changed = position_snapshot_command()
    changed["accounts"][0]["ledger_entries"][0]["cost_basis_delta"] = "900"
    changed["accounts"][0]["positions"][0]["reported_cost_basis"] = "910"
    second = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "historical-ledger-mutated", changed),
    )

    assert second.report is not None
    assert second.report.result.position is not None
    assert "LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in second.report.result.position.conflicts
    }

    repeated = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "historical-ledger-mutated-repeat", changed),
    )
    assert repeated.report is not None
    assert repeated.report.result.position is not None
    assert "LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in repeated.report.result.position.conflicts
    }

    restored = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "historical-ledger-restored-original",
            position_snapshot_command(),
        ),
    )
    assert restored.report is not None
    assert restored.report.result.position is not None
    assert restored.report.result.position.disposition == "RECONCILED"


def test_position_snapshot_retains_closed_ledger_history_without_missing_summary_conflict(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["ledger_entries"].extend(
        [
            {
                "entry_id": "synthetic-closed-buy",
                "entry_type": "FILL",
                "security_id": "CLOSED-4017",
                "quantity_delta": "1",
                "cost_basis_delta": "10",
                "cash_delta": "-10",
                "occurred_at": "2042-05-16T15:01:00Z",
                "evidence": position_evidence("synthetic-closed-buy"),
            },
            {
                "entry_id": "synthetic-closed-sell",
                "entry_type": "FILL",
                "security_id": "CLOSED-4017",
                "quantity_delta": "-1",
                "cost_basis_delta": "-10",
                "cash_delta": "10",
                "occurred_at": "2042-05-16T15:02:00Z",
                "evidence": position_evidence("synthetic-closed-sell"),
            },
        ]
    )
    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "closed-history", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    assert "BROKER_POSITION_SUMMARY_MISSING" not in {
        conflict.code for conflict in execution.report.result.position.conflicts
    }


def test_position_snapshot_blocks_inconsistent_cash_layers_for_every_account(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["cash_state"]["transferable_cash"] = "96"

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "cash-layer-conflict", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "CASH_AVAILABILITY_LAYERS_INCONSISTENT" in {
        conflict.code for conflict in outcome.conflicts
    }
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )


def test_position_snapshot_rejects_contradictory_final_sellable_components(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["positions"][0]["frozen_quantity"] = "100"

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "sellable-component-conflict", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "BROKER_SELLABLE_QUANTITY_CONTRADICTS_ENCUMBRANCES" in {
        conflict.code for conflict in outcome.conflicts
    }
    assert outcome.snapshot.action_units[0].exact_statistical_action_quantity is None


def test_position_snapshot_accepts_append_only_historical_ledger_correction(
    migrated_settings: Settings,
) -> None:
    original = position_snapshot_command()
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "ledger-correction-original", original),
    )
    assert first.report is not None

    corrected = position_snapshot_command()
    corrected["accounts"][0]["positions"][0]["reported_cost_basis"] = "890"
    corrected["accounts"][0]["ledger_entries"].append(
        {
            "entry_id": "synthetic-fee-correction-4017-xqz",
            "entry_type": "FEE",
            "security_id": "XQZ-4017",
            "quantity_delta": "0",
            "cost_basis_delta": "-10",
            "cash_delta": "0",
            "occurred_at": "2042-05-16T15:00:04Z",
            "corrects_entry_id": "synthetic-fee-4017-xqz",
            "evidence": position_evidence("synthetic-broker-4017-fee-correction"),
        }
    )
    second = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "ledger-correction", corrected),
    )

    assert second.report is not None
    assert second.report.result.position is not None
    assert second.report.result.position.disposition == "RECONCILED"
    assert (
        second.report.result.position.snapshot.authoritative_ledger[-3].corrects_entry_id
        == "synthetic-fee-4017-xqz"
    )


def test_position_snapshot_rejects_removed_historical_ledger_entry(
    migrated_settings: Settings,
) -> None:
    original = position_snapshot_command()
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "ledger-removal-original", original),
    )
    assert first.report is not None

    removed = position_snapshot_command()
    removed["accounts"][0]["ledger_entries"].pop(1)
    removed["accounts"][0]["positions"][0]["reported_cost_basis"] = "890"
    second = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "ledger-removal", removed),
    )

    assert second.report is not None
    assert second.report.result.position is not None
    assert "LEDGER_ENTRY_REMOVED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in second.report.result.position.conflicts
    }


def test_position_snapshot_does_not_borrow_shadow_ledger_history(
    migrated_settings: Settings,
) -> None:
    shadow = position_snapshot_command()
    shadow["accounts"][0]["positions"][0]["security_id"] = "SHADOW-ONLY-4017"
    shadow["accounts"][0]["open_orders"][0]["security_id"] = "SHADOW-ONLY-4017"
    shadow["accounts"][0]["ledger_entries"][0]["security_id"] = "SHADOW-ONLY-4017"
    shadow["accounts"][0]["ledger_entries"][1]["security_id"] = "SHADOW-ONLY-4017"
    shadow["annotations"][0]["security_id"] = "SHADOW-ONLY-4017"
    shadow_payload = position_case_payload(migrated_settings, "shadow-ledger-history", shadow)
    shadow_payload["access_scope"]["visibility"] = "SHADOW"

    shadow_execution = run_frozen_decision_case(migrated_settings, shadow_payload)
    assert shadow_execution.report is None

    user_execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "user-after-shadow-ledger-history",
            position_snapshot_command(),
        ),
    )

    assert user_execution.report is not None
    assert user_execution.report.result.position is not None
    assert user_execution.report.result.position.disposition == "RECONCILED"
    assert "SHADOW-ONLY-4017" not in user_execution.report.model_dump_json()


def test_position_snapshot_keeps_immutable_ledger_evidence_across_later_cutoff(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "immutable-ledger-evidence-original",
            position_snapshot_command(),
        ),
    )
    assert original.report is not None

    later_cutoff = "2042-05-18T16:00:00Z"
    later = position_snapshot_command()
    later["snapshot_id"] = "synthetic-position-snapshot-beta"
    later["cutoff_at"] = later_cutoff
    refresh_current_position_evidence(later, later_cutoff)
    cash = later["accounts"][0]["cash_state"]
    cash["ledger_cash"] = "150"
    cash["trading_cash"] = "145"
    cash["transferable_cash"] = "140"
    later["accounts"][0]["account_equity"] = "1360"
    later["accounts"][0]["ledger_entries"].append(
        {
            "entry_id": "synthetic-transfer-4017-cash-next-cutoff",
            "entry_type": "TRANSFER_IN",
            "security_id": None,
            "quantity_delta": "0",
            "cost_basis_delta": "0",
            "cash_delta": "50",
            "occurred_at": "2042-05-18T15:00:00Z",
            "evidence": position_evidence(
                "synthetic-broker-4017-transfer-next-cutoff",
                cutoff_at=later_cutoff,
            ),
        }
    )

    later_execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "immutable-ledger-evidence-next", later),
    )

    assert later_execution.report is not None
    assert later_execution.report.result.position is not None
    outcome = later_execution.report.result.position
    assert outcome.disposition == "RECONCILED"
    ledger_by_id = {entry.entry_id: entry for entry in outcome.snapshot.authoritative_ledger}
    original_cutoff = ledger_by_id["synthetic-fill-4017-xqz"].evidence.cutoff_at
    next_cutoff = ledger_by_id["synthetic-transfer-4017-cash-next-cutoff"].evidence.cutoff_at
    assert original_cutoff is not None
    assert next_cutoff is not None
    assert original_cutoff.isoformat() == "2042-05-17T16:00:00+00:00"
    assert next_cutoff.isoformat() == "2042-05-18T16:00:00+00:00"


def test_position_snapshot_blocks_all_quantities_when_a_portfolio_total_is_missing(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["positions"][0]["total_quantity"] = None

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "portfolio-total-missing", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "TOTAL_QUANTITY_MISSING" in {conflict.code for conflict in outcome.conflicts}
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )
