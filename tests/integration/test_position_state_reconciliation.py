from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest

from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    FrozenDecisionCase,
    load_frozen_decision_case,
)
from stock_profiler.modules.delivery.access import AccessPrincipal


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


def position_evidence(source: str) -> dict[str, str]:
    return {
        "source": source,
        "business_effective_at": "2042-05-17T15:00:00Z",
        "source_observed_at": "2042-05-17T15:10:00Z",
        "locally_acquired_at": "2042-05-17T15:14:00Z",
        "validated_at": "2042-05-17T15:18:00Z",
        "cutoff_at": "2042-05-17T16:00:00Z",
    }


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
                "account_equity": "1000",
                "account_equity_evidence": position_evidence("synthetic-broker-4017-equity"),
                "cash_state": {
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
                        "issuer_id": "FICTIONAL-ORBITAL-MOSAIC",
                        "security_id": "XQZ-4017",
                        "total_quantity": "100",
                        "broker_sellable_quantity": "70",
                        "sellable_quantity_semantics": "BROKER_FINAL_SELLABLE",
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
                        "issuer_id": "FICTIONAL-ORBITAL-MOSAIC",
                        "security_id": "XQZ-4017",
                        "total_quantity": "50",
                        "broker_sellable_quantity": "45",
                        "sellable_quantity_semantics": "BROKER_FINAL_SELLABLE",
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
    assert outcome.snapshot.total_account_equity == Decimal("1800")
    assert len(outcome.snapshot.action_units) == 2
    assert [unit.account_id for unit in outcome.snapshot.action_units] == [
        "synthetic-account-4017",
        "synthetic-account-8029",
    ]
    assert outcome.snapshot.action_units[0].total_quantity == Decimal("100")
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
    assert outcome.snapshot.action_units[1].exact_statistical_action_quantity == Decimal("45")
    assert outcome.snapshot.action_units[1].exact_quantity_status == "AVAILABLE"
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
    assert all(
        conflict.affected_scope.account_id == "synthetic-account-4017"
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
