from __future__ import annotations

import json
import sys
from calendar import monthrange
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

import pytest
from test_authenticated_report_delivery import _authenticated_client_with_csrf
from test_investable_universe import manifest, universe_payload
from test_scoped_qualification import GovernanceClock, case_payload

from stock_profiler.bootstrap import decision_cases as case_bootstrap
from stock_profiler.bootstrap.decision_cases import (
    correct_default_frozen_decision_case,
    replay_default_frozen_decision_case,
    run_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.cli import main
from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import FrameworkRunResult


def selection_payload(
    settings: Settings, *, security_count: int = 12, universe_blocked: bool = False
) -> dict[str, Any]:
    source = universe_payload(settings)
    universe = source["universe"]
    template = universe["securities"][0]
    universe["securities"] = [
        {**deepcopy(template), "security_id": f"XQZ-SELECT-{index:03}"}
        for index in range(security_count)
    ]
    universe["security_inventory"] = [row["security_id"] for row in universe["securities"]]
    universe["manifest"] = manifest(universe)
    if universe_blocked:
        rejected = json.loads(
            (
                Path(__file__).parents[1] / "fixtures/synthetic/result-families/input-rejected.json"
            ).read_text()
        )
        source["input"] = rejected["input"]
        source["expected_external_result"] = rejected["expected_external_result"]
    source_run = run_frozen_decision_case(
        settings, source, clock=GovernanceClock("2042-05-30T16:02:00Z")
    )
    assert source_run.report is not None
    payload = case_payload(settings, "selection", {}, contract_version="selection.1.0.0")
    payload.pop("governance")
    payload["knowledge_cutoff"] = source["knowledge_cutoff"]
    payload["report_generated_at"] = "2042-05-31T00:04:00+08:00"
    payload["selection"] = {
        "contract_version": "1.0.0",
        "synthetic": True,
        "generator_version": "fictional-selection/1",
        "seed": 918,
        "cutoff_at": source["knowledge_cutoff"],
        "purpose": "SYNTHETIC",
        "universe_object_id": source_run.report.business_object_id,
        "universe_event_id": source_run.report.event_id,
        "policy": {
            "version_id": "synthetic-selection-policy-v1",
            "cohort_size": 6,
            "industry_limit": 2,
            "capitalization_limit": 3,
            "correlation_sessions": 8,
            "maximum_correlation": "0.7",
            "positive_weight": 3,
            "terminal_weight": 2,
        },
        "industry_version": "synthetic-industry-v1",
        "adjustment_version": "synthetic-adjustment-v1",
        "screening_snapshot_id": "synthetic-dual-head-snapshot-v1",
        "strategy_version": "synthetic-dual-head-strategy-v1",
        "rows": [
            {
                "security_id": security["security_id"],
                "industry": f"synthetic-industry-{index % 6}",
                "float_capitalization": str(1000 + index),
                "positive_score": str(12 - index),
                "terminal_score": str(12 - index),
                "adjusted_returns": [
                    str(((index + 1) * (day + 3) % 13 - 6) / 100) for day in range(8)
                ],
                "return_dates": [f"2042-05-{day:02}" for day in range(23, 31)],
            }
            for index, security in enumerate(universe["securities"])
        ],
    }
    bind_selection_evidence(payload, universe)
    return payload


def bind_selection_evidence(
    payload: dict[str, Any],
    universe: dict[str, Any] | None = None,
    *,
    rebuild_screening: bool = True,
) -> None:
    selection = payload["selection"]
    if universe is not None:
        evidence = deepcopy(manifest(universe)["entries"][0]["evidence"])
    else:
        evidence = selection["evidence"]
    if rebuild_screening:
        universe_policy = (
            universe["policy"]["version_id"]
            if universe is not None
            else selection["screening"]["strategy"]["data_contracts"]["universe_policy"]
        )
        bind_screening_snapshot(selection, universe_policy)
    content = json.dumps(
        {
            key: selection[key]
            for key in (
                "rows",
                "industry_version",
                "adjustment_version",
                "screening_snapshot_id",
                "strategy_version",
                "policy",
                "screening",
            )
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence["content"] = content
    evidence["content_sha256"] = sha256(content.encode()).hexdigest()
    evidence["authority"] = "CERTIFIED_DELIVERY"
    selection["evidence"] = evidence
    rows = selection["rows"]
    payloads = {
        "industry": {
            "version": selection["industry_version"],
            "rows": [
                {"security_id": row["security_id"], "industry": row["industry"]} for row in rows
            ],
        },
        "capitalization": [
            {"security_id": row["security_id"], "float_capitalization": row["float_capitalization"]}
            for row in rows
        ],
        "adjusted_returns": {
            "version": selection["adjustment_version"],
            "rows": [
                {key: row[key] for key in ("security_id", "return_dates", "adjusted_returns")}
                for row in rows
            ],
        },
    }
    for definition in selection["screening"]["strategy"]["signals"]:
        payloads[f"signal:{definition['signal_id']}"] = {
            "semantics_version": definition["semantics_version"],
            "rows": [
                {
                    "security_id": row["security_id"],
                    "values": [
                        value
                        for value in row["signals"]
                        if value["signal_id"] == definition["signal_id"]
                    ],
                }
                for row in selection["screening"]["observations"]
            ],
        }
    entries = []
    for family, value in payloads.items():
        fact = deepcopy(evidence)
        fact["authority"] = "CERTIFIED_DELIVERY" if family.startswith("signal:") else "EXCHANGE"
        fact["content"] = json.dumps(value, sort_keys=True, separators=(",", ":"))
        fact["content_sha256"] = sha256(fact["content"].encode()).hexdigest()
        entries.append(
            {
                "field_family": family,
                "requirement": "REQUIRED",
                "semantics_version": selection["screening"]["strategy"]["data_contracts"].get(
                    f"field:{family}", f"synthetic-{family}-v1"
                ),
                "primary_source": fact["source"],
                "evidence": fact,
                "substitution": None,
            }
        )
    selection["manifest"] = {"version_id": "synthetic-selection-manifest-v1", "entries": entries}


def bind_screening_snapshot(selection: dict[str, Any], universe_policy: str) -> None:
    def digest(value: object) -> str:
        return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    features = ("synthetic-positive-signal", "synthetic-terminal-signal")
    strategy = {
        "model_family": "RESTRICTED_ADDITIVE_BINARY",
        "curve_basis": "PIECEWISE_LINEAR",
        "positive_target": "synthetic-positive-label-v1",
        "terminal_target": "synthetic-terminal-label-v1",
        "signals": [
            {
                "signal_id": key,
                "semantics_version": f"{key}-v1",
                "population": "UNIVERSE",
                "reverse": False,
                "monotone": True,
                "minimum_states": [],
            }
            for key in features
        ],
        "interactions": [],
        "knots": ["0", "100"],
        "maximum_interaction_rank": 1,
        "selection_policy_sha256": digest(selection["policy"]),
        "training_policy_version": "synthetic-training-window-v1",
        "regularization_version": "synthetic-fixed-penalty-v1",
        "minimum_mature_months": 2,
        "rolling_mature_months": 4,
        "training_weighting": "MONTH_EQUAL_STOCK_EQUAL",
        "regularization_strength": "0.5",
        "training_start_month": "2041-01",
        "training_calendar_version": "synthetic-training-calendar-v1",
        "label_horizon_months": 15,
        "data_contracts": {
            "industry": selection["industry_version"],
            "adjustment": selection["adjustment_version"],
            "universe_policy": universe_policy,
            "manifest": "synthetic-selection-manifest-v1",
            **{
                f"field:{family}": f"synthetic-{family}-v1"
                for family in ("industry", "capitalization", "adjusted_returns")
            },
            **{f"field:signal:{key}": f"{key}-v1" for key in features},
        },
        "label_contract_version": "synthetic-label-contract-v1",
        "entry_contract_version": "synthetic-entry-contract-v1",
        "cost_contract_version": "synthetic-cost-contract-v1",
        "evaluation_contract_version": "synthetic-evaluation-contract-v1",
        "qualification_contract_version": "synthetic-qualification-contract-v1",
    }
    strategy["version_id"] = f"sha256:{digest(strategy)}"
    observations = [
        {
            "security_id": row["security_id"],
            "industry": row["industry"] or "synthetic-missing",
            "signals": [
                {"signal_id": key, "raw": row[field], "state": "OBSERVED"}
                for key, field in zip(features, ("positive_score", "terminal_score"), strict=True)
            ],
        }
        for row in selection["rows"]
    ]
    heads = []
    for key, field in zip(features, ("positive_score", "terminal_score"), strict=True):
        values = [Decimal(row[field]) for row in selection["rows"] if row[field] is not None]
        low, high = min(values, default=Decimal(0)), max(values, default=Decimal(0))
        heads.append(
            {
                "intercept": "0",
                "interactions": {},
                "main_effects": {
                    feature: [str(low), str(high)] if feature == key else ["0", "0"]
                    for feature in features
                },
            }
        )
    training_members = [
        {
            "month": month,
            "security_id": f"synthetic-training-{index}",
            "positive_label": True,
            "terminal_label": False,
            "label_available_at": "2042-05-29T10:00:00+08:00",
        }
        for index, month in enumerate(("2041-01", "2041-02"))
    ]
    artifact = {
        "strategy_sha256": digest(strategy),
        "fitted_at": "2042-05-29T12:00:00+08:00",
        "label_available_through": "2042-05-29T11:00:00+08:00",
        "training_months": ["2041-01", "2041-02"],
        "training_calendar": [
            {
                "month": "2041-01",
                "selection_cutoff_at": "2041-01-31T23:59:59+08:00",
                "calendar": monthly_training_calendar("2041-01"),
            },
            {
                "month": "2041-02",
                "selection_cutoff_at": "2041-02-28T23:59:59+08:00",
                "calendar": monthly_training_calendar("2041-02"),
            },
        ],
        "training_members_sha256": digest(training_members),
        "training_members": training_members,
        "environment_sha256": digest("synthetic-environment"),
        "diagnostics_sha256": digest({"status": "CONVERGED", "kind": "SYNTHETIC_PARAMETERS"}),
        "fit_diagnostics": {"status": "CONVERGED", "kind": "SYNTHETIC_PARAMETERS"},
        "positive": heads[0],
        "terminal": heads[1],
    }
    artifact["snapshot_id"] = f"sha256:{digest(artifact)}"
    selection["strategy_version"] = strategy["version_id"]
    selection["screening_snapshot_id"] = artifact["snapshot_id"]
    selection["screening"] = {
        "strategy": strategy,
        "artifact": artifact,
        "observations": observations,
    }


def monthly_training_calendar(month: str) -> dict[str, Any]:
    year, number = map(int, month.split("-"))
    return {
        "version_id": "synthetic-training-calendar-v1",
        "days": [
            {
                "market_date": f"{month}-{day:02}",
                "close_at": f"{month}-{day:02}T15:00:00+08:00",
            }
            for day in range(1, monthrange(year, number)[1] + 1)
        ],
    }


def test_selection_freezes_ranked_members_from_committed_universe(
    migrated_settings: Settings,
) -> None:
    payload = selection_payload(migrated_settings)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["selection"]
    assert result["disposition"] == "FROZEN"
    assert len(result["members"]) == 6
    assert len(result["ranking"]) == 12
    assert result["ranking"][0]["security_id"] == "XQZ-SELECT-000"
    assert result["ranking"][-1]["security_id"] == "XQZ-SELECT-011"
    assert result["ranking"][0]["positive_percentile"] == "100"
    assert result["ranking"][-1]["positive_percentile"] == "0"
    assert result["universe_event_id"] == payload["selection"]["universe_event_id"]
    assert result["population"]["valid_monthly"] is True
    assert result["population"]["recommendation_coverage_denominator"] is True
    assert result["actionable"] is False
    assert execution.business_commit_status == "COMMITTED"
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
        )
        == execution
    )


def test_mixed_head_composite_ties_use_security_identity(migrated_settings: Settings) -> None:
    payload = selection_payload(migrated_settings)
    positive = [2, 0, 1, *range(3, 12)]
    terminal = [5, 8, *[value for value in range(12) if value not in {5, 8}]]
    for row, left, right in zip(payload["selection"]["rows"], positive, terminal, strict=True):
        row["positive_score"] = str(left)
        row["terminal_score"] = str(right)
    bind_selection_evidence(payload)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None and execution.report.result.selection is not None
    ranks = {row.security_id: row for row in execution.report.result.selection.ranking}
    assert ranks["XQZ-SELECT-000"].composite_score == ranks["XQZ-SELECT-001"].composite_score
    assert ranks["XQZ-SELECT-000"].rank < ranks["XQZ-SELECT-001"].rank


def test_selection_policy_is_bound_to_screening_evidence(migrated_settings: Settings) -> None:
    payload = selection_payload(migrated_settings)
    payload["selection"]["policy"]["positive_weight"] += 1
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None and execution.report.result.selection is not None
    assert execution.report.result.selection.disposition == "DATA_FAILED"


def test_blocked_universe_does_not_become_data_failure(migrated_settings: Settings) -> None:
    payload = selection_payload(migrated_settings, universe_blocked=True)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None and execution.report.result.selection is not None
    result = execution.report.result.selection
    assert result.disposition == "BLOCKED"
    assert result.population.availability_failure is None
    assert not result.population.valid_monthly
    assert any(
        stage.phase == "BUSINESS_DECISION" and stage.status == "REJECTED"
        for stage in execution.report.stage_results
    )


def test_preclose_selection_evidence_cannot_claim_complete_daily_returns(
    migrated_settings: Settings,
) -> None:
    payload = selection_payload(migrated_settings)
    for key in (
        "fact_effective_at",
        "source_published_at",
        "source_observed_at",
        "acquired_at",
        "validated_at",
    ):
        payload["selection"]["evidence"][key] = "2042-05-30T09:00:00+08:00"
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None and execution.report.result.selection is not None
    assert execution.report.result.selection.disposition == "DATA_FAILED"
    assert "SELECTION_MARKET_CLOSE_NOT_OBSERVED" in execution.report.result.selection.reasons


def test_selection_requires_field_specific_manifest(migrated_settings: Settings) -> None:
    payload = selection_payload(migrated_settings)
    payload["selection"].pop("manifest", None)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None and execution.report.result.selection is not None
    assert execution.report.result.selection.disposition == "DATA_FAILED"
    assert "SELECTION_MANIFEST_REQUIRED" in execution.report.result.selection.reasons


def test_selection_requires_replayable_screening_snapshot(migrated_settings: Settings) -> None:
    payload = selection_payload(migrated_settings)
    payload["selection"]["screening"] = None
    payload["selection"]["manifest"]["entries"] = [
        entry
        for entry in payload["selection"]["manifest"]["entries"]
        if not entry["field_family"].startswith("signal:")
    ]
    evidence = payload["selection"]["evidence"]
    content = json.loads(evidence["content"])
    content["screening"] = None
    evidence["content"] = json.dumps(content, sort_keys=True, separators=(",", ":"))
    evidence["content_sha256"] = sha256(evidence["content"].encode()).hexdigest()
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None and execution.report.result.selection is not None
    assert execution.report.result.selection.disposition == "SYSTEM_FAILED"
    assert execution.report.result.selection.population.availability_failure == "SYSTEM"


@pytest.mark.parametrize("gate", ["INDUSTRY_LIMIT", "CAPITALIZATION_LIMIT", "CORRELATION_LIMIT"])
def test_selection_scans_constraints_in_rank_order(migrated_settings: Settings, gate: str) -> None:
    payload = selection_payload(migrated_settings)
    selection = payload["selection"]
    rows = selection["rows"]
    selection["policy"]["maximum_correlation"] = "1"
    if gate == "INDUSTRY_LIMIT":
        for row in rows[:3]:
            row["industry"] = "synthetic-shared-industry"
        skipped = "XQZ-SELECT-002"
    elif gate == "CAPITALIZATION_LIMIT":
        skipped = "XQZ-SELECT-003"
    else:
        selection["policy"]["maximum_correlation"] = "0.99"
        rows[1]["adjusted_returns"] = rows[0]["adjusted_returns"]
        skipped = "XQZ-SELECT-001"
    bind_selection_evidence(payload)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["selection"]
    assert result["disposition"] == "FROZEN"
    assert len(result["members"]) == 6
    assert skipped not in result["members"]
    step = next(step for step in result["scan"] if step["security_id"] == skipped)
    assert gate in step["reasons"]


def test_infeasible_industry_constraints_abstain_without_partial_members(
    migrated_settings: Settings,
) -> None:
    payload = selection_payload(migrated_settings)
    for row in payload["selection"]["rows"]:
        row["industry"] = "synthetic-only-industry"
    bind_selection_evidence(payload)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["selection"]
    assert result["disposition"] == "ABSTAINED"
    assert result["members"] == []
    assert len(result["ranking"]) == 12
    assert result["population"]["valid_monthly"] is True
    assert result["population"]["recommendation_coverage_denominator"] is True
    assert result["population"]["selection_pass_denominator"] is True
    assert result["population"]["selection_pass"] is False
    assert result["population"]["availability_failure"] is None
    assert any(stage.status == "ABSTAINED" for stage in execution.report.stage_results)


@pytest.mark.parametrize(
    "defect",
    [
        "missing_member",
        "duplicate_member",
        "unknown_member",
        "industry",
        "capitalization",
        "head",
        "returns",
        "dates",
        "constant",
        "manifest",
        "late",
        "hash",
        "source",
        "source_event",
        "purpose",
        "invalid_return",
    ],
)
def test_selection_data_defects_fail_the_whole_month(
    migrated_settings: Settings, defect: str
) -> None:
    payload = selection_payload(migrated_settings)
    selection = payload["selection"]
    row = selection["rows"][-1]
    if defect == "missing_member":
        selection["rows"].pop()
    elif defect == "duplicate_member":
        selection["rows"].append(deepcopy(row))
    elif defect == "unknown_member":
        row["security_id"] = "XQZ-UNRECOGNIZED"
    elif defect == "industry":
        row["industry"] = None
    elif defect == "capitalization":
        row["float_capitalization"] = "0"
    elif defect == "head":
        row["terminal_score"] = None
    elif defect == "returns":
        row["adjusted_returns"].pop()
    elif defect == "dates":
        row["return_dates"][-1] = "2042-05-31"
    elif defect == "constant":
        row["adjusted_returns"] = ["0"] * 8
    elif defect == "invalid_return":
        row["adjusted_returns"][-1] = "-1.01"
    elif defect == "source":
        selection["universe_object_id"] = "synthetic-missing-universe"
    elif defect == "source_event":
        selection["universe_event_id"] = "synthetic-wrong-event"
    elif defect == "purpose":
        selection["purpose"] = "REAL_CANDIDATE"
    bind_selection_evidence(payload)
    if defect == "manifest":
        selection["evidence"] = None
    elif defect == "late":
        selection["evidence"]["validated_at"] = "2042-05-31T00:00:00+08:00"
    elif defect == "hash":
        selection["evidence"]["content_sha256"] = "0" * 64
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["selection"]
    assert result["disposition"] == "DATA_FAILED"
    assert result["members"] == []
    assert result["ranking"] == []
    assert result["population"]["valid_monthly"] is False
    assert result["population"]["recommendation_coverage_denominator"] is False
    assert result["population"]["scheduled_monthly"] is True
    assert result["population"]["availability_failure"] == "DATA"
    assert any(
        stage.phase == "BUSINESS_DECISION" and stage.status == "FAILED"
        for stage in execution.report.stage_results
    )


@pytest.mark.parametrize(
    "fixture,status,disposition",
    [
        ("result-failed", "FAILED", "SYSTEM_FAILED"),
        ("input-rejected", "REJECTED", "BLOCKED"),
        ("result-abstained", "ABSTAINED", "BLOCKED"),
    ],
)
def test_selection_does_not_promote_failed_prerequisites(
    migrated_settings: Settings, fixture: str, status: str, disposition: str
) -> None:
    payload = selection_payload(migrated_settings)
    original = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/synthetic/result-families" / f"{fixture}.json"
        ).read_text()
    )
    payload["input"] = original["input"]
    payload["expected_external_result"] = original["expected_external_result"]
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.business_result_status == status
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["selection"]
    assert result["disposition"] == disposition
    assert result["members"] == []
    assert result["population"]["valid_monthly"] is False
    assert result["population"]["availability_failure"] == (
        "SYSTEM" if status == "FAILED" else None
    )


@pytest.mark.parametrize(
    "field", ["industry_version", "adjustment_version", "screening_snapshot_id", "strategy_version"]
)
def test_selection_evidence_binds_versions(migrated_settings: Settings, field: str) -> None:
    payload = selection_payload(migrated_settings)
    payload["selection"][field] = "synthetic-substituted-version"
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["selection"]
    assert result["disposition"] == "DATA_FAILED"
    assert "SOURCE_FACT_MISMATCH" in result["reasons"]


@pytest.mark.parametrize("correlation", ["equal", "above", "negative"])
def test_positive_correlation_boundary_is_exact(
    migrated_settings: Settings, correlation: str
) -> None:
    payload = selection_payload(migrated_settings)
    selection = payload["selection"]
    selection["policy"]["maximum_correlation"] = "0.6"
    selection["rows"][0]["adjusted_returns"] = ["0.01", "-0.01", "0.01", "-0.01"] * 2
    selection["rows"][1]["adjusted_returns"] = ["0.07", "0.01", "-0.01", "-0.07"] * 2
    if correlation == "above":
        selection["rows"][1]["adjusted_returns"] = ["0.071", "0.009", "-0.009", "-0.071"] * 2
    elif correlation == "negative":
        selection["rows"][1]["adjusted_returns"] = ["-0.01", "0.01", "-0.01", "0.01"] * 2
    bind_selection_evidence(payload)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["selection"]
    step = next(item for item in result["scan"] if item["security_id"] == "XQZ-SELECT-001")
    assert ("CORRELATION_LIMIT" in step["reasons"]) == (correlation == "above")


def test_retry_cannot_change_frozen_identity_cutoff_or_members(migrated_settings: Settings) -> None:
    payload = selection_payload(migrated_settings)
    clock = GovernanceClock("2042-05-30T16:05:00Z")
    original = run_frozen_decision_case(migrated_settings, payload, clock=clock)
    changed = deepcopy(payload)
    changed["case_id"] = "synthetic-changed-selection-case"
    changed["business_identity"] = "synthetic:changed-selection-identity"
    changed["knowledge_cutoff"] = "2042-05-31T23:59:59+08:00"
    changed["selection"]["cutoff_at"] = changed["knowledge_cutoff"]
    changed["selection"]["rows"].reverse()
    changed["selection"]["policy"]["cohort_size"] = 2
    assert run_frozen_decision_case(migrated_settings, changed, clock=clock) == original


def test_equal_head_scores_use_shared_midrank_then_security_id(migrated_settings: Settings) -> None:
    payload = selection_payload(migrated_settings)
    for row in payload["selection"]["rows"]:
        row["positive_score"] = "4"
        row["terminal_score"] = "9"
        row["float_capitalization"] = "2000"
    payload["selection"]["rows"].reverse()
    bind_selection_evidence(payload)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["selection"]
    assert [row["security_id"] for row in result["ranking"]] == [
        f"XQZ-SELECT-{index:03}" for index in range(12)
    ]
    assert {Decimal(row["composite_score"]) for row in result["ranking"]} == {Decimal(50)}


@pytest.mark.parametrize("security_count", [0, 1, 5])
def test_small_complete_universe_abstains(migrated_settings: Settings, security_count: int) -> None:
    payload = selection_payload(migrated_settings, security_count=security_count)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None
    selection = execution.report.result.selection
    assert selection is not None
    assert selection.disposition == "ABSTAINED"
    assert selection.members == ()
    assert len(selection.ranking) == security_count


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"authority": "EXPLORATORY"}, "SELECTION_SOURCE_NOT_AUTHORITATIVE"),
        ({"fact_effective_at": "2042-04-30T15:00:00+08:00"}, "SELECTION_FACT_STALE"),
    ],
)
def test_selection_cannot_use_exploratory_or_stale_snapshot(
    migrated_settings: Settings, changes: dict[str, str], reason: str
) -> None:
    payload = selection_payload(migrated_settings)
    payload["selection"]["evidence"].update(changes)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None and execution.report.result.selection is not None
    assert execution.report.result.selection.disposition == "DATA_FAILED"
    assert reason in execution.report.result.selection.reasons


def test_selection_authenticated_readback_and_correction_retain_members(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = selection_payload(migrated_settings)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert execution.report is not None
    assert execution.report.result.selection is not None
    client, _csrf = _authenticated_client_with_csrf(migrated_settings, monkeypatch)
    with client:
        response = client.get(f"/api/v1/reports/{execution.report.report_version_id}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["result"]["selection"] == (
            execution.report.result.selection.model_dump(mode="json")
        )
    assert (
        replay_default_frozen_decision_case(
            migrated_settings,
            payload["business_identity"],
            clock=GovernanceClock("2042-06-01T16:05:00Z"),
        )
        == execution
    )
    correction = correct_default_frozen_decision_case(
        migrated_settings,
        business_identity=payload["business_identity"],
        clock=GovernanceClock("2042-06-01T16:05:00Z"),
    )
    assert correction.report.result.selection == execution.report.result.selection


def test_selection_cli_replays_and_corrects_saved_case(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = selection_payload(migrated_settings)
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert original.report is not None
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: migrated_settings)
    for command in ("decision-case-replay", "decision-case-correct"):
        monkeypatch.setattr(
            sys,
            "argv",
            ["stock-profiler", command, "--business-identity", payload["business_identity"]],
        )
        main()
        result = json.loads(capsys.readouterr().out)
        assert result["report"]["result"]["selection"] == (
            original.report.result.selection.model_dump(mode="json")
            if original.report.result.selection is not None
            else None
        )


def test_selection_console_rejects_ambiguous_saved_identity(
    migrated_settings: Settings,
) -> None:
    payload = selection_payload(migrated_settings)
    run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    alternate = deepcopy(payload)
    alternate["access_scope"]["account_ids"] = ["synthetic-unrelated-scope"]
    alternate["input"]["account"]["account_id"] = "synthetic-unrelated-scope"
    run_frozen_decision_case(
        migrated_settings, alternate, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    with pytest.raises(ValueError, match="ambiguous"):
        replay_default_frozen_decision_case(migrated_settings, payload["business_identity"])


@pytest.mark.parametrize("status", ["FAILED", "WAITING"])
def test_framework_failure_retains_original_month_without_selection_publication(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    status: Literal["FAILED", "WAITING"],
) -> None:
    payload = selection_payload(migrated_settings)
    case = FrozenDecisionCase.model_validate(payload)

    async def stopped_framework(*_: object, **__: object) -> FrameworkRunResult:
        return FrameworkRunResult(run_id=case.framework_run_id, status=status, output=None)

    with monkeypatch.context() as patched:
        patched.setattr(case_bootstrap, "execute_frozen_decision_case", stopped_framework)
        execution = run_frozen_decision_case(migrated_settings, payload)
        assert execution.report is None
        assert execution.publication_status == "CLOSED"
        assert execution.framework_run_status == status
        assert execution.business_commit_status == "NOT_ATTEMPTED"
        assert execution.stage_results[-1].status == status
        changed = deepcopy(payload)
        changed["business_identity"] = "synthetic:post-fault-retry"
        changed["selection"]["policy"]["cohort_size"] = 1
        retried = run_frozen_decision_case(migrated_settings, changed)
        assert retried.framework_run_id == case.framework_run_id
        assert retried.report is None
        assert retried.framework_run_status == status
