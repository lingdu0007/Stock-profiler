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
from pydantic import ValidationError

from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    FrozenDecisionCase,
    load_frozen_decision_case,
)
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.position_management import history as position_history
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


def position_snapshot_with_later_security(
    security_id: str,
    *,
    cutoff_at: str = "2042-05-18T16:00:00Z",
) -> dict[str, Any]:
    command = position_snapshot_command()
    command["snapshot_id"] = f"synthetic-position-snapshot-{security_id.lower()}"
    command["cutoff_at"] = cutoff_at
    refresh_current_position_evidence(command, cutoff_at)
    account = command["accounts"][0]
    account["positions"].append(
        {
            "position_id": f"synthetic-position-4017-{security_id.lower()}",
            "origin": "EXTERNAL",
            "lifecycle_id": f"synthetic-lifecycle-4017-{security_id.lower()}",
            "issuer_id": f"FICTIONAL-{security_id}",
            "security_id": security_id,
            "total_quantity": "1",
            "broker_sellable_quantity": "1",
            "sellable_quantity_semantics": "BROKER_FINAL_SELLABLE",
            "encumbrance_quantity_semantics": "OVERLAPPING_NON_SELLABLE",
            "unsettled_quantity": "0",
            "frozen_quantity": "0",
            "restricted_quantity": "0",
            "open_sell_order_quantity": "0",
            "reported_cost_basis": "10",
            "market_price": "10",
            "evidence": position_evidence(
                f"synthetic-broker-4017-{security_id}",
                cutoff_at=cutoff_at,
            ),
        }
    )
    account["ledger_entries"].append(
        {
            "entry_id": f"synthetic-fill-4017-{security_id.lower()}",
            "entry_type": "FILL",
            "security_id": security_id,
            "quantity_delta": "1",
            "cost_basis_delta": "10",
            "cash_delta": "-10",
            "occurred_at": f"{cutoff_at.partition('T')[0]}T15:00:00Z",
            "evidence": position_evidence(
                f"synthetic-broker-4017-{security_id}-fill",
                cutoff_at=cutoff_at,
            ),
        }
    )
    cash = account["cash_state"]
    cash["ledger_cash"] = "90"
    cash["trading_cash"] = "85"
    cash["transferable_cash"] = "80"
    return command


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
    command = position_snapshot_with_later_security("ORBITAL-SECONDARY-4017")
    command["accounts"][0]["positions"][-1]["issuer_id"] = "FICTIONAL-ORBITAL-MOSAIC"
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
    assert (
        next(
            unit
            for unit in outcome.snapshot.action_units
            if unit.security_id == "ORBITAL-SECONDARY-4017"
        ).exact_statistical_action_quantity
        is None
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


def test_position_snapshot_preserves_independent_ledger_history_beside_duplicate_entries(
    migrated_settings: Settings,
) -> None:
    duplicate = position_snapshot_command()
    duplicate["accounts"][0]["ledger_entries"].append(
        deepcopy(duplicate["accounts"][0]["ledger_entries"][2])
    )
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "duplicate-cash-entry-with-independent-ledger-fact",
            duplicate,
        ),
    )
    assert first.report is not None
    assert first.report.result.position is not None
    assert "LEDGER_ENTRY_ID_DUPLICATED" in {
        conflict.code for conflict in first.report.result.position.conflicts
    }

    mutated = position_snapshot_command()
    mutated["accounts"][0]["ledger_entries"][0]["cost_basis_delta"] = "900"
    mutated["accounts"][0]["positions"][0]["reported_cost_basis"] = "910"
    second = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "mutated-ledger-after-unrelated-duplicate",
            mutated,
        ),
    )

    assert second.report is not None
    assert second.report.result.position is not None
    assert "LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in second.report.result.position.conflicts
    }


def test_position_snapshot_preserves_canonical_ledger_variants_for_each_cutoff(
    migrated_settings: Settings,
) -> None:
    def snapshot_at(cutoff_at: str) -> dict[str, Any]:
        command = position_snapshot_command()
        command["snapshot_id"] = f"synthetic-position-snapshot-{cutoff_at.partition('T')[0]}"
        command["cutoff_at"] = cutoff_at
        refresh_current_position_evidence(command, cutoff_at)
        for account in command["accounts"]:
            for entry in account["ledger_entries"]:
                entry["evidence"] = position_evidence(
                    entry["evidence"]["source"],
                    cutoff_at=cutoff_at,
                )
        return command

    for cutoff_at in (
        "2042-05-20T16:00:00Z",
        "2042-05-18T16:00:00Z",
        "2042-05-17T16:00:00Z",
    ):
        execution = run_frozen_decision_case(
            migrated_settings,
            position_case_payload(
                migrated_settings,
                f"canonical-ledger-variant-{cutoff_at.partition('T')[0]}",
                snapshot_at(cutoff_at),
            ),
        )
        assert execution.report is not None
        assert execution.report.result.position is not None
        assert execution.report.result.position.disposition == "RECONCILED"

    replacement = snapshot_at("2042-05-17T16:00:00Z")
    replacement["accounts"][0]["ledger_entries"] = [
        entry
        for entry in replacement["accounts"][0]["ledger_entries"]
        if entry["entry_id"] != "synthetic-fill-4017-xqz"
    ]
    rejected = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "canonical-ledger-variant-rejected-may17-replacement",
            replacement,
        ),
    )
    assert rejected.report is not None
    assert rejected.report.result.position is not None
    assert "LEDGER_ENTRY_REMOVED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in rejected.report.result.position.conflicts
    }

    later = snapshot_at("2042-05-19T16:00:00Z")
    for account in later["accounts"]:
        for entry in account["ledger_entries"]:
            entry["evidence"] = position_evidence(
                entry["evidence"]["source"],
                cutoff_at="2042-05-18T16:00:00Z",
            )
    replay = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "canonical-ledger-variant-may19-replay",
            later,
        ),
    )

    assert replay.report is not None
    assert replay.report.result.position is not None
    assert replay.report.result.position.disposition == "RECONCILED"
    assert "LEDGER_ENTRY_REMOVED_ACROSS_SNAPSHOTS" not in {
        conflict.code for conflict in replay.report.result.position.conflicts
    }


def test_position_snapshot_rejects_additions_after_an_intermediate_cutoff_mutation(
    migrated_settings: Settings,
) -> None:
    def snapshot_at(cutoff_at: str) -> dict[str, Any]:
        command = position_snapshot_command()
        command["snapshot_id"] = f"synthetic-position-snapshot-{cutoff_at.partition('T')[0]}"
        command["cutoff_at"] = cutoff_at
        refresh_current_position_evidence(command, cutoff_at)
        for account in command["accounts"]:
            for entry in account["ledger_entries"]:
                entry["evidence"] = position_evidence(
                    entry["evidence"]["source"],
                    cutoff_at=cutoff_at,
                )
        return command

    for cutoff_at in ("2042-05-20T16:00:00Z", "2042-05-18T16:00:00Z"):
        execution = run_frozen_decision_case(
            migrated_settings,
            position_case_payload(
                migrated_settings,
                f"intermediate-ledger-variant-{cutoff_at.partition('T')[0]}",
                snapshot_at(cutoff_at),
            ),
        )
        assert execution.report is not None
        assert execution.report.result.position is not None
        assert execution.report.result.position.disposition == "RECONCILED"

    rejected_command = snapshot_at("2042-05-18T16:00:00Z")
    rejected_command["accounts"][0]["ledger_entries"][0]["cost_basis_delta"] = "900"
    rejected_command["accounts"][0]["positions"][0]["reported_cost_basis"] = "910"
    rejected_command["accounts"][0]["ledger_entries"].append(
        {
            "entry_id": "synthetic-rejected-intermediate-addition",
            "entry_type": "TRANSFER_IN",
            "security_id": None,
            "quantity_delta": "0",
            "cost_basis_delta": "0",
            "cash_delta": "0",
            "occurred_at": "2042-05-16T15:00:04Z",
            "evidence": position_evidence(
                "synthetic-rejected-intermediate-addition",
                cutoff_at="2042-05-18T16:00:00Z",
            ),
        }
    )
    rejected = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "intermediate-ledger-rejected-mutation",
            rejected_command,
        ),
    )
    assert rejected.report is not None
    assert rejected.report.result.position is not None
    assert "LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in rejected.report.result.position.conflicts
    }

    replay_command = snapshot_at("2042-05-21T16:00:00Z")
    for account in replay_command["accounts"]:
        for entry in account["ledger_entries"]:
            entry["evidence"] = position_evidence(
                entry["evidence"]["source"],
                cutoff_at="2042-05-20T16:00:00Z",
            )
    replay = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "intermediate-ledger-may21-replay",
            replay_command,
        ),
    )

    assert replay.report is not None
    assert replay.report.result.position is not None
    assert replay.report.result.position.disposition == "RECONCILED"
    assert "LEDGER_ENTRY_REMOVED_ACROSS_SNAPSHOTS" not in {
        conflict.code for conflict in replay.report.result.position.conflicts
    }


def test_position_snapshot_preserves_earlier_ledger_evidence_from_a_later_snapshot(
    migrated_settings: Settings,
) -> None:
    later = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "later-snapshot-first",
            position_snapshot_with_later_security("FUTURE-ONLY-4017"),
        ),
    )
    assert later.report is not None
    assert later.report.result.position is not None
    assert later.report.result.position.disposition == "RECONCILED"

    backdated_mutation = position_snapshot_command()
    backdated_mutation["accounts"][0]["ledger_entries"][0]["cost_basis_delta"] = "900"
    backdated_mutation["accounts"][0]["positions"][0]["reported_cost_basis"] = "910"
    earlier = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "earlier-snapshot-mutated",
            backdated_mutation,
        ),
    )

    assert earlier.report is not None
    assert earlier.report.result.position is not None
    assert "LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in earlier.report.result.position.conflicts
    }


def test_position_snapshot_detects_backdated_ledger_mutation_after_future_fact(
    migrated_settings: Settings,
) -> None:
    future_command = position_snapshot_with_later_security("FUTURE-INVISIBLE-LEDGER-4017")
    for account in future_command["accounts"]:
        for entry in account["ledger_entries"]:
            entry["evidence"] = position_evidence(
                entry["evidence"]["source"],
                cutoff_at=future_command["cutoff_at"],
            )
    future = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-before-backdated-ledger-original",
            future_command,
        ),
    )
    assert future.report is not None

    original = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "backdated-ledger-original-after-future",
            position_snapshot_command(),
        ),
    )
    assert original.report is not None
    assert original.report.result.position is not None
    assert original.report.result.position.disposition == "RECONCILED"

    mutated = position_snapshot_command()
    mutated["accounts"][0]["ledger_entries"][0]["cost_basis_delta"] = "900"
    mutated["accounts"][0]["positions"][0]["reported_cost_basis"] = "910"
    replay = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "backdated-ledger-mutation-after-future",
            mutated,
        ),
    )

    assert replay.report is not None
    assert replay.report.result.position is not None
    assert "LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in replay.report.result.position.conflicts
    }


def test_position_snapshot_does_not_preserve_rejected_ledger_replacements(
    migrated_settings: Settings,
) -> None:
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "ledger-replacement-original",
            position_snapshot_command(),
        ),
    )
    assert first.report is not None

    replacement = position_snapshot_command()
    replacement["accounts"][0]["ledger_entries"][0]["entry_id"] = (
        "synthetic-unlinked-fill-replacement"
    )
    conflicted = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "ledger-replacement-conflicted",
            replacement,
        ),
    )
    assert conflicted.report is not None
    assert conflicted.report.result.position is not None
    assert "LEDGER_ENTRY_REMOVED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in conflicted.report.result.position.conflicts
    }

    restored = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "ledger-replacement-restored",
            position_snapshot_command(),
        ),
    )

    assert restored.report is not None
    assert restored.report.result.position is not None
    assert restored.report.result.position.disposition == "RECONCILED"


def test_position_snapshot_does_not_preserve_future_rejected_ledger_replacements(
    migrated_settings: Settings,
) -> None:
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-ledger-replacement-original",
            position_snapshot_command(),
        ),
    )
    assert first.report is not None

    replacement = position_snapshot_command()
    replacement["snapshot_id"] = "synthetic-position-snapshot-future-replacement"
    replacement["cutoff_at"] = "2042-05-18T16:00:00Z"
    refresh_current_position_evidence(replacement, replacement["cutoff_at"])
    replacement_entry = replacement["accounts"][0]["ledger_entries"][0]
    replacement_entry["entry_id"] = "synthetic-future-unlinked-fill-replacement"
    replacement_entry["evidence"] = position_evidence("synthetic-broker-4017-fill")
    conflicted = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-ledger-replacement-conflicted",
            replacement,
        ),
    )
    assert conflicted.report is not None
    assert conflicted.report.result.position is not None
    assert "LEDGER_ENTRY_REMOVED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in conflicted.report.result.position.conflicts
    }

    replayed = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-ledger-replacement-replayed-original",
            position_snapshot_command(),
        ),
    )

    assert replayed.report is not None
    assert replayed.report.result.position is not None
    assert replayed.report.result.position.disposition == "RECONCILED"


def test_position_snapshot_does_not_admit_rejected_backdated_entries_at_earlier_cutoff(
    migrated_settings: Settings,
) -> None:
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-canonical-ledger-original",
            position_snapshot_with_later_security("RETIRED-MAY18-4017"),
        ),
    )
    assert first.report is not None

    rejected = position_snapshot_with_later_security("BACKDATED-MAY17-4017")
    backdated_entry = rejected["accounts"][0]["ledger_entries"][-1]
    backdated_entry["occurred_at"] = "2042-05-17T15:00:00Z"
    backdated_entry["evidence"] = position_evidence("synthetic-broker-4017-backdated-may17-fill")
    conflicted = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-canonical-ledger-rejected-backdated",
            rejected,
        ),
    )
    assert conflicted.report is not None
    assert conflicted.report.result.position is not None
    assert "LEDGER_ENTRY_REMOVED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in conflicted.report.result.position.conflicts
    }

    replayed = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-canonical-ledger-replayed-original",
            position_snapshot_command(),
        ),
    )

    assert replayed.report is not None
    assert replayed.report.result.position is not None
    assert replayed.report.result.position.disposition == "RECONCILED"


def test_position_snapshot_preserves_valid_backdated_additions_after_future_snapshot(
    migrated_settings: Settings,
) -> None:
    future = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-before-backdated-addition",
            position_snapshot_with_later_security("FUTURE-TRANSFER-4017"),
        ),
    )
    assert future.report is not None

    backdated = position_snapshot_command()
    account = backdated["accounts"][0]
    account["ledger_entries"].append(
        {
            "entry_id": "synthetic-backdated-transfer-in",
            "entry_type": "TRANSFER_IN",
            "security_id": None,
            "quantity_delta": "0",
            "cost_basis_delta": "0",
            "cash_delta": "10",
            "occurred_at": "2042-05-16T15:00:04Z",
            "evidence": position_evidence("synthetic-backdated-transfer-in"),
        }
    )
    account["cash_state"]["ledger_cash"] = "110"
    account["cash_state"]["trading_cash"] = "105"
    account["cash_state"]["transferable_cash"] = "100"
    account["account_equity"] = "1320"
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "backdated-addition-original",
            backdated,
        ),
    )
    assert first.report is not None
    assert first.report.result.position is not None
    assert first.report.result.position.disposition == "RECONCILED"

    mutated = position_snapshot_command()
    account = mutated["accounts"][0]
    account["ledger_entries"].append(
        {
            "entry_id": "synthetic-backdated-transfer-in",
            "entry_type": "TRANSFER_IN",
            "security_id": None,
            "quantity_delta": "0",
            "cost_basis_delta": "0",
            "cash_delta": "11",
            "occurred_at": "2042-05-16T15:00:04Z",
            "evidence": position_evidence("synthetic-backdated-transfer-in"),
        }
    )
    account["cash_state"]["ledger_cash"] = "111"
    account["cash_state"]["trading_cash"] = "106"
    account["cash_state"]["transferable_cash"] = "101"
    account["account_equity"] = "1321"
    second = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "backdated-addition-mutated",
            mutated,
        ),
    )

    assert second.report is not None
    assert second.report.result.position is not None
    assert "LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in second.report.result.position.conflicts
    }


def test_position_snapshot_retains_valid_ledger_history_from_unrelated_conflicted_snapshot(
    migrated_settings: Settings,
) -> None:
    unrelated_conflict = position_snapshot_command()
    unrelated_conflict["accounts"][1]["positions"][0]["broker_sellable_quantity"] = None
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "unrelated-conflicted-ledger-history",
            unrelated_conflict,
        ),
    )
    assert first.report is not None
    assert first.report.result.position is not None
    assert first.report.result.position.disposition == "CONFLICTED"

    mutated = position_snapshot_command()
    mutated["accounts"][0]["ledger_entries"][0]["cost_basis_delta"] = "900"
    mutated["accounts"][0]["positions"][0]["reported_cost_basis"] = "910"
    second = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "valid-ledger-after-unrelated-conflict",
            mutated,
        ),
    )

    assert second.report is not None
    assert second.report.result.position is not None
    assert "LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in second.report.result.position.conflicts
    }


def test_position_snapshot_retains_valid_ledger_entry_beside_invalid_same_security_entry(
    migrated_settings: Settings,
) -> None:
    first_command = position_snapshot_command()
    first_command["accounts"][0]["ledger_entries"][1]["evidence"]["source"] = None
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "invalid-fee-valid-fill-ledger-history",
            first_command,
        ),
    )
    assert first.report is not None
    assert first.report.result.position is not None
    assert first.report.result.position.disposition == "CONFLICTED"

    mutated = position_snapshot_command()
    mutated["accounts"][0]["ledger_entries"][0]["cost_basis_delta"] = "900"
    mutated["accounts"][0]["positions"][0]["reported_cost_basis"] = "910"
    second = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "mutated-fill-after-invalid-fee",
            mutated,
        ),
    )

    assert second.report is not None
    assert second.report.result.position is not None
    assert "LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in second.report.result.position.conflicts
    }


def test_position_snapshot_rejects_mutated_opening_cash_baseline(
    migrated_settings: Settings,
) -> None:
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "opening-cash-original",
            position_snapshot_command(),
        ),
    )
    assert first.report is not None

    changed = position_snapshot_command()
    account = changed["accounts"][0]
    account["cash_state"]["opening_ledger_cash"] = "1902"
    account["cash_state"]["ledger_cash"] = "1100"
    account["cash_state"]["trading_cash"] = "1095"
    account["cash_state"]["transferable_cash"] = "1090"
    account["account_equity"] = "2310"
    second = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "opening-cash-mutated", changed),
    )

    assert second.report is not None
    assert second.report.result.position is not None
    outcome = second.report.result.position
    assert "OPENING_LEDGER_CASH_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in outcome.conflicts
    }
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )


def test_position_snapshot_detects_backdated_opening_cash_mutation_after_future_fact(
    migrated_settings: Settings,
) -> None:
    future = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-before-backdated-opening-cash-original",
            position_snapshot_with_later_security("FUTURE-INVISIBLE-CASH-4017"),
        ),
    )
    assert future.report is not None

    original = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "backdated-opening-cash-original-after-future",
            position_snapshot_command(),
        ),
    )
    assert original.report is not None
    assert original.report.result.position is not None
    assert original.report.result.position.disposition == "RECONCILED"

    mutated = position_snapshot_command()
    account = mutated["accounts"][0]
    account["cash_state"]["opening_ledger_cash"] = "1902"
    account["cash_state"]["ledger_cash"] = "1100"
    account["cash_state"]["trading_cash"] = "1095"
    account["cash_state"]["transferable_cash"] = "1090"
    account["account_equity"] = "2310"
    replay = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "backdated-opening-cash-mutation-after-future",
            mutated,
        ),
    )

    assert replay.report is not None
    assert replay.report.result.position is not None
    assert "OPENING_LEDGER_CASH_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in replay.report.result.position.conflicts
    }


def test_position_snapshot_does_not_promote_rejected_backdated_opening_cash(
    migrated_settings: Settings,
) -> None:
    future = position_snapshot_with_later_security("FUTURE-CASH-4017")
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-opening-cash-original",
            future,
        ),
    )
    assert first.report is not None

    replacement = position_snapshot_with_later_security("FUTURE-CASH-4017")
    account = replacement["accounts"][0]
    cash = account["cash_state"]
    cash["opening_ledger_cash"] = "1902"
    cash["opening_ledger_cash_evidence"] = position_evidence("synthetic-broker-4017-opening-cash")
    cash["ledger_cash"] = "1100"
    cash["trading_cash"] = "1095"
    cash["transferable_cash"] = "1090"
    account["account_equity"] = "2310"
    rejected = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-opening-cash-rejected-replacement",
            replacement,
        ),
    )
    assert rejected.report is not None
    assert rejected.report.result.position is not None
    assert "OPENING_LEDGER_CASH_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in rejected.report.result.position.conflicts
    }

    replayed = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-opening-cash-replayed-original",
            position_snapshot_command(),
        ),
    )

    assert replayed.report is not None
    assert replayed.report.result.position is not None
    assert replayed.report.result.position.disposition == "RECONCILED"


def test_position_snapshot_retains_opening_cash_baseline_beside_incomplete_cash_layers(
    migrated_settings: Settings,
) -> None:
    first_command = position_snapshot_command()
    first_command["accounts"][0]["cash_state"]["trading_cash"] = None
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "incomplete-layer-opening-cash-original",
            first_command,
        ),
    )
    assert first.report is not None
    assert first.report.result.position is not None
    assert first.report.result.position.disposition == "CONFLICTED"

    changed = position_snapshot_command()
    account = changed["accounts"][0]
    account["cash_state"]["opening_ledger_cash"] = "1902"
    account["cash_state"]["ledger_cash"] = "1100"
    account["cash_state"]["trading_cash"] = "1095"
    account["cash_state"]["transferable_cash"] = "1090"
    account["account_equity"] = "2310"
    second = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "incomplete-layer-opening-cash-mutated",
            changed,
        ),
    )

    assert second.report is not None
    assert second.report.result.position is not None
    assert "OPENING_LEDGER_CASH_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in second.report.result.position.conflicts
    }


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
            "correction_reason": "Broker fee correction.",
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
    assert (
        second.report.result.position.snapshot.authoritative_ledger[-3].correction_reason
        == "Broker fee correction."
    )


def test_position_snapshot_requires_a_reason_for_each_ledger_correction() -> None:
    command = position_snapshot_command()
    command["accounts"][0]["ledger_entries"].append(
        {
            "entry_id": "synthetic-correction-without-reason",
            "entry_type": "FEE",
            "security_id": "XQZ-4017",
            "quantity_delta": "0",
            "cost_basis_delta": "0",
            "cash_delta": "0",
            "occurred_at": "2042-05-16T15:00:04Z",
            "corrects_entry_id": "synthetic-fee-4017-xqz",
            "evidence": position_evidence("synthetic-correction-without-reason"),
        }
    )

    with pytest.raises(ValidationError, match="correction_reason"):
        PositionSnapshotCommand.model_validate(command)


def test_position_history_preindexes_ledger_validity_per_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = PositionSnapshotCommand.model_validate(position_snapshot_command())
    outcome = reconcile(command)
    visible_call_count = 0
    original_visibility_check = position_history.ledger_entry_evidence_is_visible

    def counted_visibility_check(*args: Any, **kwargs: Any) -> bool:
        nonlocal visible_call_count
        visible_call_count += 1
        return original_visibility_check(*args, **kwargs)

    monkeypatch.setattr(
        position_history,
        "ledger_entry_evidence_is_visible",
        counted_visibility_check,
    )

    history = position_history.authoritative_ledger_history(
        (outcome,) * 100,
        account_ids=frozenset(command.account_ids),
        cutoff_at=command.cutoff_at,
    )

    assert history == outcome.snapshot.authoritative_ledger
    assert visible_call_count == len(outcome.snapshot.authoritative_ledger) * 100


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


def test_position_snapshot_never_projects_partial_exposure_after_historical_holding_removal(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "historical-holding-removal-original",
            position_snapshot_command(),
        ),
    )
    assert original.report is not None

    removed = position_snapshot_command()
    account = removed["accounts"][0]
    account["positions"] = []
    account["open_orders"] = []
    account["ledger_entries"] = [
        entry for entry in account["ledger_entries"] if entry["security_id"] is None
    ]
    account["account_equity"] = "110"
    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "historical-holding-removal",
            removed,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "LEDGER_ENTRY_REMOVED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in outcome.conflicts
    }
    assert all(
        exposure.current_market_exposure is None for exposure in outcome.snapshot.issuer_exposures
    )


def test_position_snapshot_never_splits_issuer_exposure_after_historical_security_mutation(
    migrated_settings: Settings,
) -> None:
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "historical-security-mutation-original",
            position_snapshot_command(),
        ),
    )
    assert first.report is not None

    changed = position_snapshot_command()
    account = changed["accounts"][0]
    replacement_security_id = "REPLACEMENT-SECURITY-4017"
    replacement_issuer_id = "FICTIONAL-REPLACEMENT-ISSUER"
    account["positions"][0]["security_id"] = replacement_security_id
    account["positions"][0]["issuer_id"] = replacement_issuer_id
    account["open_orders"][0]["security_id"] = replacement_security_id
    for entry in account["ledger_entries"][:2]:
        entry["security_id"] = replacement_security_id
    changed["annotations"][0]["security_id"] = replacement_security_id
    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "historical-security-mutation",
            changed,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "LEDGER_ENTRY_MUTATED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in outcome.conflicts
    }
    assert {
        exposure.issuer_id: exposure.current_market_exposure
        for exposure in outcome.snapshot.issuer_exposures
    } == {
        "FICTIONAL-ORBITAL-MOSAIC": None,
        "FICTIONAL-REPLACEMENT-ISSUER": None,
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


def test_position_snapshot_ignores_later_ledger_history_for_an_earlier_cutoff(
    migrated_settings: Settings,
) -> None:
    future_security_id = "FUTURE-ONLY-4017"
    future = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "future-ledger-history",
            position_snapshot_with_later_security(future_security_id),
        ),
    )
    assert future.report is not None
    assert future.report.result.position is not None
    assert future.report.result.position.disposition == "RECONCILED"

    earlier = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "earlier-after-future-ledger-history",
            position_snapshot_command(),
        ),
    )

    assert earlier.report is not None
    assert earlier.report.result.position is not None
    assert earlier.report.result.position.disposition == "RECONCILED"
    assert future_security_id not in earlier.report.model_dump_json()


def test_position_snapshot_retains_overlapping_account_ledger_history(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "overlapping-ledger-history-original",
            position_snapshot_command(),
        ),
    )
    assert original.report is not None

    narrowed = position_snapshot_command()
    narrowed["accounts"] = [narrowed["accounts"][0]]
    narrowed_account = narrowed["accounts"][0]
    narrowed_account["ledger_entries"].pop(1)
    narrowed_account["positions"][0]["reported_cost_basis"] = "890"
    narrowed_account["account_equity"] = "1320"
    narrowed_cash = narrowed_account["cash_state"]
    narrowed_cash["ledger_cash"] = "110"
    narrowed_cash["trading_cash"] = "105"
    narrowed_cash["transferable_cash"] = "100"
    narrowed_execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "overlapping-ledger-history-narrowed",
            narrowed,
        ),
    )

    assert narrowed_execution.report is not None
    assert narrowed_execution.report.result.position is not None
    assert "LEDGER_ENTRY_REMOVED_ACROSS_SNAPSHOTS" in {
        conflict.code for conflict in narrowed_execution.report.result.position.conflicts
    }


def test_position_snapshot_blocks_all_quantities_for_ledger_quantity_mismatch(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["positions"][0]["total_quantity"] = "101"
    command["accounts"][0]["account_equity"] = "1322"

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "ledger-quantity-conflict", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "LEDGER_QUANTITY_MISMATCH" in {conflict.code for conflict in outcome.conflicts}
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )


def test_position_snapshot_blocks_all_quantities_for_missing_position_summary(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["positions"] = []
    command["accounts"][0]["account_equity"] = "110"

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "summary-missing-global-block", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "BROKER_POSITION_SUMMARY_MISSING" in {conflict.code for conflict in outcome.conflicts}
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )


def test_position_snapshot_never_projects_partial_issuer_exposure_across_missing_security_codes(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_with_later_security("ORBITAL-UNSUMMARIZED-4017")
    command["accounts"][0]["positions"][-1]["issuer_id"] = "FICTIONAL-ORBITAL-MOSAIC"
    command["accounts"][0]["positions"].pop()
    command["accounts"][0]["account_equity"] = "1300"

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "issuer-missing-code-summary", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "BROKER_POSITION_SUMMARY_MISSING" in {conflict.code for conflict in outcome.conflicts}
    assert all(
        exposure.current_market_exposure is None for exposure in outcome.snapshot.issuer_exposures
    )


def test_position_snapshot_never_projects_exposure_after_an_incomplete_account_manifest(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    account["snapshot_evidence"]["complete_through_at"] = "2042-05-17T15:59:59Z"
    account["positions"] = []
    account["open_orders"] = []
    account["ledger_entries"] = []
    account["cash_state"]["opening_ledger_cash"] = "100"
    account["cash_state"]["ledger_cash"] = "100"
    account["cash_state"]["trading_cash"] = "95"
    account["cash_state"]["transferable_cash"] = "90"
    account["cash_state"]["frozen_cash"] = "5"
    account["account_equity"] = "110"

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "incomplete-account-manifest", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "FACT_COMPLETENESS_WATERMARK_INSUFFICIENT" in {
        conflict.code for conflict in outcome.conflicts
    }
    assert all(
        exposure.current_market_exposure is None for exposure in outcome.snapshot.issuer_exposures
    )


def test_position_snapshot_rejects_duplicate_broker_order_identity(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["open_orders"][0]["remaining_quantity"] = "5"
    duplicate = deepcopy(command["accounts"][0]["open_orders"][0])
    duplicate["remaining_quantity"] = "10"
    command["accounts"][0]["open_orders"].append(duplicate)

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "duplicate-open-order", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "OPEN_ORDER_ID_DUPLICATED" in {conflict.code for conflict in outcome.conflicts}
    assert outcome.snapshot.action_units[0].exact_statistical_action_quantity is None


@pytest.mark.parametrize("known_order_first", [False, True])
def test_position_snapshot_requires_summary_for_every_duplicate_sell_order_security(
    migrated_settings: Settings,
    known_order_first: bool,
) -> None:
    command = position_snapshot_command()
    known_order = deepcopy(command["accounts"][0]["open_orders"][0])
    known_order.update(
        {
            "side": "SELL",
            "remaining_quantity": "15",
            "reserved_cash": None,
            "reserved_cash_semantics": "NOT_APPLICABLE",
        }
    )
    missing_order = deepcopy(known_order)
    missing_order["security_id"] = "MISSING-DUPLICATE-SELL-4017"
    command["accounts"][0]["open_orders"] = (
        [known_order, missing_order] if known_order_first else [missing_order, known_order]
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            f"duplicate-sell-order-summary-{known_order_first}",
            command,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert {
        "OPEN_ORDER_ID_DUPLICATED",
        "BROKER_POSITION_SUMMARY_MISSING",
    } <= {conflict.code for conflict in outcome.conflicts}
    assert outcome.snapshot.total_account_equity is None
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )
    assert all(
        exposure.current_market_exposure is None for exposure in outcome.snapshot.issuer_exposures
    )


@pytest.mark.parametrize("duplicate_entry_first", [False, True])
def test_position_snapshot_blocks_issuer_exposure_for_every_duplicate_ledger_identity(
    migrated_settings: Settings,
    duplicate_entry_first: bool,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    account["positions"].append(
        {
            "position_id": "synthetic-position-4017-duplicate-secondary",
            "origin": "EXTERNAL",
            "lifecycle_id": "synthetic-lifecycle-4017-duplicate-secondary",
            "issuer_id": "FICTIONAL-DUPLICATE-SECONDARY",
            "security_id": "DUPLICATE-SECONDARY-4017",
            "total_quantity": "1",
            "broker_sellable_quantity": "1",
            "sellable_quantity_semantics": "BROKER_FINAL_SELLABLE",
            "encumbrance_quantity_semantics": "OVERLAPPING_NON_SELLABLE",
            "unsettled_quantity": "0",
            "frozen_quantity": "0",
            "restricted_quantity": "0",
            "open_sell_order_quantity": "0",
            "reported_cost_basis": "10",
            "market_price": "10",
            "evidence": position_evidence("synthetic-broker-4017-duplicate-secondary"),
        }
    )
    duplicate_entry = {
        "entry_id": "synthetic-fill-4017-xqz",
        "entry_type": "FILL",
        "security_id": "DUPLICATE-SECONDARY-4017",
        "quantity_delta": "1",
        "cost_basis_delta": "10",
        "cash_delta": "-10",
        "occurred_at": "2042-05-16T15:00:04Z",
        "evidence": position_evidence("synthetic-broker-4017-duplicate-secondary-fill"),
    }
    if duplicate_entry_first:
        account["ledger_entries"].insert(0, duplicate_entry)
    else:
        account["ledger_entries"].append(duplicate_entry)
    cash = account["cash_state"]
    cash["ledger_cash"] = "90"
    cash["trading_cash"] = "85"
    cash["transferable_cash"] = "80"

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            f"duplicate-ledger-identity-{duplicate_entry_first}",
            command,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "LEDGER_ENTRY_ID_DUPLICATED" in {conflict.code for conflict in outcome.conflicts}
    assert {
        exposure.issuer_id: exposure.current_market_exposure
        for exposure in outcome.snapshot.issuer_exposures
    } == {
        "FICTIONAL-DUPLICATE-SECONDARY": None,
        "FICTIONAL-ORBITAL-MOSAIC": None,
    }


@pytest.mark.parametrize(
    "duplicate_sides",
    [
        ("SELL", "SELL", "BUY"),
        ("SELL", "BUY", "SELL"),
    ],
)
def test_position_snapshot_applies_duplicate_order_portfolio_blocking_independent_of_row_order(
    migrated_settings: Settings,
    duplicate_sides: tuple[str, str, str],
) -> None:
    command = position_snapshot_command()
    original = command["accounts"][0]["open_orders"][0]
    command["accounts"][0]["open_orders"] = [
        {
            **original,
            "side": side,
            "remaining_quantity": "15" if side == "SELL" else "1",
            "reserved_cash": None if side == "SELL" else "0",
            "reserved_cash_semantics": (
                "NOT_APPLICABLE" if side == "SELL" else "BROKER_FINAL_RESERVED_CASH"
            ),
        }
        for side in duplicate_sides
    ]

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            f"duplicate-order-{'-'.join(duplicate_sides).lower()}",
            command,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "OPEN_ORDER_ID_DUPLICATED" in {conflict.code for conflict in outcome.conflicts}
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )


@pytest.mark.parametrize(
    ("entry_ids", "targets", "expected_code"),
    [
        (
            ("synthetic-self-correction",),
            ("synthetic-self-correction",),
            "LEDGER_CORRECTION_SELF_REFERENCE",
        ),
        (
            ("synthetic-cycle-correction-a", "synthetic-cycle-correction-b"),
            ("synthetic-cycle-correction-b", "synthetic-cycle-correction-a"),
            "LEDGER_CORRECTION_CYCLE",
        ),
    ],
)
def test_position_snapshot_rejects_invalid_ledger_correction_lineage(
    migrated_settings: Settings,
    entry_ids: tuple[str, ...],
    targets: tuple[str, ...],
    expected_code: str,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["ledger_entries"].extend(
        {
            "entry_id": entry_id,
            "entry_type": "CORPORATE_ACTION",
            "security_id": None,
            "quantity_delta": "0",
            "cost_basis_delta": "0",
            "cash_delta": "0",
            "occurred_at": "2042-05-16T15:00:04Z",
            "corrects_entry_id": target,
            "correction_reason": "Synthetic lineage validation.",
            "evidence": position_evidence(f"synthetic-broker-{entry_id}"),
        }
        for entry_id, target in zip(entry_ids, targets, strict=True)
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, f"invalid-correction-{expected_code}", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    assert expected_code in {
        conflict.code for conflict in execution.report.result.position.conflicts
    }


def test_position_snapshot_does_not_preserve_correction_with_invalid_ancestor(
    migrated_settings: Settings,
) -> None:
    invalid_lineage = position_snapshot_command()
    invalid_lineage["accounts"][0]["ledger_entries"].extend(
        [
            {
                "entry_id": "synthetic-invalid-correction-ancestor",
                "entry_type": "CORPORATE_ACTION",
                "security_id": "XQZ-4017",
                "quantity_delta": "0",
                "cost_basis_delta": "0",
                "cash_delta": "0",
                "occurred_at": "2042-05-16T15:00:04Z",
                "corrects_entry_id": "synthetic-correction-target-not-present",
                "correction_reason": "Synthetic unresolved correction.",
                "evidence": position_evidence("synthetic-invalid-correction-ancestor"),
            },
            {
                "entry_id": "synthetic-invalid-correction-descendant",
                "entry_type": "CORPORATE_ACTION",
                "security_id": "XQZ-4017",
                "quantity_delta": "0",
                "cost_basis_delta": "0",
                "cash_delta": "0",
                "occurred_at": "2042-05-16T15:00:05Z",
                "corrects_entry_id": "synthetic-invalid-correction-ancestor",
                "correction_reason": "Synthetic descendant correction.",
                "evidence": position_evidence("synthetic-invalid-correction-descendant"),
            },
        ]
    )
    first = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "invalid-correction-ancestor",
            invalid_lineage,
        ),
    )
    assert first.report is not None
    assert first.report.result.position is not None
    assert "LEDGER_CORRECTION_TARGET_MISSING" in {
        conflict.code for conflict in first.report.result.position.conflicts
    }

    replayed = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "invalid-correction-ancestor-replayed-original",
            position_snapshot_command(),
        ),
    )

    assert replayed.report is not None
    assert replayed.report.result.position is not None
    assert replayed.report.result.position.disposition == "RECONCILED"


def test_position_snapshot_rejects_ledger_evidence_before_its_fill(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_with_later_security("CAUSAL-4017")
    command["accounts"][0]["ledger_entries"][-1]["evidence"] = position_evidence(
        "synthetic-broker-4017-causal-fill"
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "ledger-evidence-precedes-fill", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "LEDGER_ENTRY_EVIDENCE_PRECEDES_OCCURRENCE" in {
        conflict.code for conflict in outcome.conflicts
    }
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )


def test_position_snapshot_rejects_security_bearing_ledger_entry_without_security_identity(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["ledger_entries"].append(
        {
            "entry_id": "synthetic-unidentified-fill-4017",
            "entry_type": "FILL",
            "security_id": None,
            "quantity_delta": "123",
            "cost_basis_delta": "456",
            "cash_delta": "0",
            "occurred_at": "2042-05-16T15:00:04Z",
            "evidence": position_evidence("synthetic-broker-unidentified-fill"),
        }
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "unidentified-ledger-fill", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "LEDGER_ENTRY_SECURITY_ID_MISSING" in {conflict.code for conflict in outcome.conflicts}
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )
    assert all(
        exposure.current_market_exposure is None for exposure in outcome.snapshot.issuer_exposures
    )


@pytest.mark.parametrize("invalid_closure", ["evidence", "duplicate_identity"])
def test_position_snapshot_requires_a_summary_for_unverified_closed_ledger_security(
    migrated_settings: Settings,
    invalid_closure: str,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    entry_id = f"synthetic-unverified-closed-{invalid_closure}"
    opening_entry: dict[str, Any] = {
        "entry_id": entry_id,
        "entry_type": "FILL",
        "security_id": "UNVERIFIED-CLOSED-4017",
        "quantity_delta": "1",
        "cost_basis_delta": "10",
        "cash_delta": "-10",
        "occurred_at": "2042-05-16T15:00:04Z",
        "evidence": position_evidence("synthetic-unverified-closed-opening"),
    }
    closing_entry: dict[str, Any] = {
        "entry_id": (
            entry_id
            if invalid_closure == "duplicate_identity"
            else "synthetic-unverified-closed-evidence"
        ),
        "entry_type": "FILL",
        "security_id": "UNVERIFIED-CLOSED-4017",
        "quantity_delta": "-1",
        "cost_basis_delta": "-10",
        "cash_delta": "10",
        "occurred_at": "2042-05-16T15:00:05Z",
        "evidence": position_evidence("synthetic-unverified-closed-closing"),
    }
    if invalid_closure == "evidence":
        closing_entry["evidence"]["source"] = None
    account["ledger_entries"].extend((opening_entry, closing_entry))

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            f"unverified-closed-ledger-{invalid_closure}",
            command,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "BROKER_POSITION_SUMMARY_MISSING" in {conflict.code for conflict in outcome.conflicts}
    assert all(
        exposure.current_market_exposure is None for exposure in outcome.snapshot.issuer_exposures
    )


def test_position_snapshot_requires_a_summary_for_invalid_closing_correction(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    account["ledger_entries"].extend(
        [
            {
                "entry_id": "synthetic-invalid-correction-opening",
                "entry_type": "FILL",
                "security_id": "INVALID-CORRECTION-CLOSED-4017",
                "quantity_delta": "1",
                "cost_basis_delta": "10",
                "cash_delta": "-10",
                "occurred_at": "2042-05-16T15:00:04Z",
                "evidence": position_evidence("synthetic-invalid-correction-opening"),
            },
            {
                "entry_id": "synthetic-invalid-correction-closing",
                "entry_type": "FILL",
                "security_id": "INVALID-CORRECTION-CLOSED-4017",
                "quantity_delta": "-1",
                "cost_basis_delta": "-10",
                "cash_delta": "10",
                "occurred_at": "2042-05-16T15:00:05Z",
                "corrects_entry_id": "synthetic-correction-target-not-present",
                "correction_reason": "Synthetic unresolved correction.",
                "evidence": position_evidence("synthetic-invalid-correction-closing"),
            },
        ]
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "invalid-correction-closed-ledger",
            command,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert {
        "LEDGER_CORRECTION_TARGET_MISSING",
        "BROKER_POSITION_SUMMARY_MISSING",
    } <= {conflict.code for conflict in outcome.conflicts}
    assert all(
        exposure.current_market_exposure is None for exposure in outcome.snapshot.issuer_exposures
    )


def test_position_snapshot_requires_a_summary_for_a_correction_with_invalid_cash_ancestor(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    account["ledger_entries"].extend(
        [
            {
                "entry_id": "synthetic-invalid-cash-ancestor",
                "entry_type": "FEE",
                "security_id": None,
                "quantity_delta": "0",
                "cost_basis_delta": "0",
                "cash_delta": "0",
                "occurred_at": "2042-05-16T15:00:04Z",
                "evidence": {
                    **position_evidence("synthetic-invalid-cash-ancestor"),
                    "source": None,
                },
            },
            {
                "entry_id": "synthetic-unresolved-correction-opening",
                "entry_type": "FILL",
                "security_id": "UNRESOLVED-CORRECTION-4017",
                "quantity_delta": "1",
                "cost_basis_delta": "10",
                "cash_delta": "-10",
                "occurred_at": "2042-05-16T15:00:05Z",
                "evidence": position_evidence("synthetic-unresolved-correction-opening"),
            },
            {
                "entry_id": "synthetic-unresolved-correction-closing",
                "entry_type": "FILL",
                "security_id": "UNRESOLVED-CORRECTION-4017",
                "quantity_delta": "-1",
                "cost_basis_delta": "-10",
                "cash_delta": "10",
                "occurred_at": "2042-05-16T15:00:06Z",
                "corrects_entry_id": "synthetic-invalid-cash-ancestor",
                "correction_reason": "Synthetic ancestor correction.",
                "evidence": position_evidence("synthetic-unresolved-correction-closing"),
            },
        ]
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "unresolved-correction-invalid-cash-ancestor",
            command,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert {
        "SOURCE_MISSING",
        "BROKER_POSITION_SUMMARY_MISSING",
    } <= {conflict.code for conflict in outcome.conflicts}
    assert all(
        exposure.current_market_exposure is None for exposure in outcome.snapshot.issuer_exposures
    )


def test_position_snapshot_hides_exposure_for_a_correction_with_invalid_cash_ancestor(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    account["ledger_entries"].extend(
        [
            {
                "entry_id": "synthetic-invalid-cash-ancestor-with-summary",
                "entry_type": "FEE",
                "security_id": None,
                "quantity_delta": "0",
                "cost_basis_delta": "0",
                "cash_delta": "0",
                "occurred_at": "2042-05-16T15:00:04Z",
                "evidence": {
                    **position_evidence("synthetic-invalid-cash-ancestor-with-summary"),
                    "source": None,
                },
            },
            {
                "entry_id": "synthetic-unresolved-correction-with-summary",
                "entry_type": "FILL",
                "security_id": "XQZ-4017",
                "quantity_delta": "-50",
                "cost_basis_delta": "-450",
                "cash_delta": "450",
                "occurred_at": "2042-05-16T15:00:05Z",
                "corrects_entry_id": "synthetic-invalid-cash-ancestor-with-summary",
                "correction_reason": "Synthetic ancestor correction.",
                "evidence": position_evidence("synthetic-unresolved-correction-with-summary"),
            },
        ]
    )
    position = account["positions"][0]
    position["total_quantity"] = "50"
    position["broker_sellable_quantity"] = "35"
    position["reported_cost_basis"] = "450"
    account["cash_state"]["ledger_cash"] = "550"
    account["cash_state"]["trading_cash"] = "545"
    account["cash_state"]["transferable_cash"] = "540"
    account["account_equity"] = "1160"

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "unresolved-correction-invalid-cash-ancestor-with-summary",
            command,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "SOURCE_MISSING" in {conflict.code for conflict in outcome.conflicts}
    assert all(
        exposure.current_market_exposure is None for exposure in outcome.snapshot.issuer_exposures
    )


def test_position_snapshot_requires_a_summary_for_a_correction_with_ambiguous_cash_ancestor(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    account["ledger_entries"].extend(
        [
            {
                "entry_id": "synthetic-ambiguous-cash-ancestor",
                "entry_type": "FEE",
                "security_id": None,
                "quantity_delta": "0",
                "cost_basis_delta": "0",
                "cash_delta": "0",
                "occurred_at": "2042-05-16T15:00:04Z",
                "evidence": position_evidence("synthetic-ambiguous-cash-ancestor-a"),
            },
            {
                "entry_id": "synthetic-ambiguous-cash-ancestor",
                "entry_type": "FEE",
                "security_id": None,
                "quantity_delta": "0",
                "cost_basis_delta": "0",
                "cash_delta": "0",
                "occurred_at": "2042-05-16T15:00:05Z",
                "evidence": position_evidence("synthetic-ambiguous-cash-ancestor-b"),
            },
            {
                "entry_id": "synthetic-ambiguous-correction-opening",
                "entry_type": "FILL",
                "security_id": "AMBIGUOUS-CORRECTION-4017",
                "quantity_delta": "1",
                "cost_basis_delta": "10",
                "cash_delta": "-10",
                "occurred_at": "2042-05-16T15:00:06Z",
                "evidence": position_evidence("synthetic-ambiguous-correction-opening"),
            },
            {
                "entry_id": "synthetic-ambiguous-correction-closing",
                "entry_type": "FILL",
                "security_id": "AMBIGUOUS-CORRECTION-4017",
                "quantity_delta": "-1",
                "cost_basis_delta": "-10",
                "cash_delta": "10",
                "occurred_at": "2042-05-16T15:00:07Z",
                "corrects_entry_id": "synthetic-ambiguous-cash-ancestor",
                "correction_reason": "Synthetic ambiguous correction.",
                "evidence": position_evidence("synthetic-ambiguous-correction-closing"),
            },
        ]
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            "unresolved-correction-ambiguous-cash-ancestor",
            command,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert {
        "LEDGER_ENTRY_ID_DUPLICATED",
        "BROKER_POSITION_SUMMARY_MISSING",
    } <= {conflict.code for conflict in outcome.conflicts}
    assert all(
        exposure.current_market_exposure is None for exposure in outcome.snapshot.issuer_exposures
    )


@pytest.mark.parametrize(
    ("account_equity", "evidence_update", "expected_code"),
    [
        ("999999", {}, "ACCOUNT_EQUITY_MISMATCH"),
        (None, {}, "ACCOUNT_EQUITY_MISSING"),
        (
            "1310",
            {"source_observed_at": "2042-05-17T14:59:00Z"},
            "EVIDENCE_CLOCK_INVALID",
        ),
    ],
)
def test_position_snapshot_does_not_publish_total_equity_from_an_unqualified_account(
    migrated_settings: Settings,
    account_equity: str | None,
    evidence_update: dict[str, str],
    expected_code: str,
) -> None:
    command = position_snapshot_command()
    account = command["accounts"][0]
    account["account_equity"] = account_equity
    account["account_equity_evidence"].update(evidence_update)

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(
            migrated_settings,
            f"unqualified-account-equity-{expected_code.lower()}",
            command,
        ),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert expected_code in {conflict.code for conflict in outcome.conflicts}
    assert outcome.snapshot.total_account_equity is None


@pytest.mark.parametrize(
    ("field", "expected_code"),
    [
        ("cash_availability_semantics", "CASH_AVAILABILITY_SEMANTICS_UNKNOWN"),
        ("ledger_cash_semantics", "LEDGER_CASH_SEMANTICS_UNKNOWN"),
    ],
)
def test_position_snapshot_retains_unknown_cash_semantics_as_conflicts(
    migrated_settings: Settings,
    field: str,
    expected_code: str,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["cash_state"][field] = "UNKNOWN"

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, f"unknown-{field}", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert expected_code in {conflict.code for conflict in outcome.conflicts}
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )


def test_position_snapshot_rejects_correction_evidence_before_its_target(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["ledger_entries"].append(
        {
            "entry_id": "synthetic-causally-impossible-fee-correction",
            "entry_type": "FEE",
            "security_id": "XQZ-4017",
            "quantity_delta": "0",
            "cost_basis_delta": "0",
            "cash_delta": "0",
            "occurred_at": "2042-05-16T15:00:04Z",
            "corrects_entry_id": "synthetic-fee-4017-xqz",
            "correction_reason": "Synthetic causal validation.",
            "evidence": position_evidence(
                "synthetic-broker-causally-impossible-fee-correction",
                cutoff_at="2042-05-15T16:00:00Z",
            ),
        }
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "causally-impossible-correction", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "LEDGER_CORRECTION_EVIDENCE_PRECEDES_TARGET" in {
        conflict.code for conflict in outcome.conflicts
    }
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )


def test_position_snapshot_rejects_frozen_cash_above_ledger_availability(
    migrated_settings: Settings,
) -> None:
    command = position_snapshot_command()
    command["accounts"][0]["cash_state"]["frozen_cash"] = "1000000"

    execution = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "frozen-cash-conflict", command),
    )

    assert execution.report is not None
    assert execution.report.result.position is not None
    outcome = execution.report.result.position
    assert "CASH_FROZEN_EXCEEDS_LEDGER_AVAILABILITY" in {
        conflict.code for conflict in outcome.conflicts
    }
    assert all(
        unit.exact_statistical_action_quantity is None for unit in outcome.snapshot.action_units
    )
