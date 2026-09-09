from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, localcontext
from hashlib import sha256
from typing import Any

import pytest
from test_investable_universe import substitution
from test_scoped_qualification import GovernanceClock
from test_selection_cohort import (
    bind_selection_evidence,
    monthly_training_calendar,
    selection_payload,
)

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.candidate_selection.screening import (
    ScreeningSnapshot,
    replay_screening,
)


def digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def seal(selection: dict[str, Any]) -> None:
    snapshot = selection["screening"]
    strategy, artifact = snapshot["strategy"], snapshot["artifact"]
    strategy["version_id"] = "sha256:" + digest(
        {key: value for key, value in strategy.items() if key != "version_id"}
    )
    artifact["strategy_sha256"] = digest(strategy)
    artifact["snapshot_id"] = "sha256:" + digest(
        {key: value for key, value in artifact.items() if key != "snapshot_id"}
    )
    selection["strategy_version"] = strategy["version_id"]
    selection["screening_snapshot_id"] = artifact["snapshot_id"]


def replay(selection: dict[str, Any]) -> Any:
    return replay_screening(
        ScreeningSnapshot.model_validate(selection["screening"]),
        cutoff=datetime.fromisoformat(selection["cutoff_at"]),
        policy=selection["policy"],
        strategy_version=selection["strategy_version"],
        snapshot_id=selection["screening_snapshot_id"],
        industries={row["security_id"]: row["industry"] for row in selection["rows"]},
        data_contracts=selection["screening"]["strategy"]["data_contracts"],
    )


@pytest.mark.parametrize(
    "defect",
    [
        "strategy",
        "snapshot",
        "policy",
        "fitted-future",
        "watermark-future",
        "training-empty",
        "training-duplicates",
        "training-hash",
        "training-label-future",
        "training-label-conflict",
        "signal-empty",
        "signal-duplicates",
        "interaction-duplicates",
        "interaction-unknown",
        "reserved-name",
        "knots-short",
        "knots-unsorted",
        "knots-start",
        "knots-end",
        "main-effect-missing",
        "curve-size",
        "monotone",
        "interaction-missing",
        "observation-duplicate",
        "observation-missing",
        "industry",
        "signal-missing",
        "signal-unavailable",
        "signal-state",
        "score",
    ],
)
def test_invalid_screening_artifact_fails_without_fallback(
    migrated_settings: Settings,
    defect: str,
) -> None:
    payload = selection_payload(migrated_settings)
    selection = payload["selection"]
    snapshot = selection["screening"]
    strategy, artifact = snapshot["strategy"], snapshot["artifact"]
    observations = snapshot["observations"]
    feature = "synthetic-positive-signal"
    if defect == "fitted-future":
        artifact["fitted_at"] = "2042-06-01T12:00:00+08:00"
    elif defect == "watermark-future":
        artifact["label_available_through"] = "2042-05-30T12:00:00+08:00"
    elif defect == "training-empty":
        artifact["training_months"] = []
    elif defect == "training-duplicates":
        artifact["training_months"].append(artifact["training_months"][0])
    elif defect == "training-hash":
        artifact["training_members_sha256"] = "0" * 64
    elif defect == "training-label-future":
        artifact["training_members"][0]["label_available_at"] = "2042-06-01T12:00:00+08:00"
        artifact["training_members_sha256"] = digest(artifact["training_members"])
    elif defect == "training-label-conflict":
        artifact["training_members"][0]["positive_label"] = False
        artifact["training_members"][0]["terminal_label"] = True
        artifact["training_members_sha256"] = digest(artifact["training_members"])
    elif defect == "signal-empty":
        strategy["signals"] = []
    elif defect == "signal-duplicates":
        strategy["signals"].append(strategy["signals"][0])
    elif defect == "interaction-duplicates":
        strategy["interactions"] = [
            {"interaction_id": "synthetic-pair", "left": feature, "right": feature}
        ] * 2
    elif defect == "interaction-unknown":
        strategy["interactions"] = [
            {"interaction_id": "synthetic-pair", "left": "unknown", "right": feature}
        ]
    elif defect == "reserved-name":
        strategy["signals"][0]["signal_id"] = "intercept"
    elif defect == "knots-short":
        strategy["knots"] = ["0"]
    elif defect == "knots-unsorted":
        strategy["knots"] = ["0", "50", "50", "100"]
    elif defect == "knots-start":
        strategy["knots"][0] = "1"
    elif defect == "knots-end":
        strategy["knots"][-1] = "99"
    elif defect == "main-effect-missing":
        artifact["positive"]["main_effects"].pop(feature)
    elif defect == "curve-size":
        artifact["positive"]["main_effects"][feature] = ["1"]
    elif defect == "monotone":
        artifact["positive"]["main_effects"][feature] = ["2", "1"]
    elif defect == "interaction-missing":
        strategy["interactions"] = [
            {"interaction_id": "synthetic-pair", "left": feature, "right": feature}
        ]
    elif defect == "observation-duplicate":
        observations.append(observations[0])
    elif defect == "observation-missing":
        observations.pop()
    elif defect == "industry":
        observations[0]["industry"] = "synthetic-other-industry"
    elif defect == "signal-missing":
        observations[0]["signals"].pop()
    elif defect == "signal-unavailable":
        observations[0]["signals"][0]["raw"] = None
    elif defect == "signal-state":
        observations[0]["signals"][0].update(raw=None, state="NON_POSITIVE_EQUITY")
    elif defect == "score":
        selection["rows"][0]["positive_score"] = "99"
    seal(selection)
    if defect == "strategy":
        selection["strategy_version"] = "synthetic-unbound-strategy"
    elif defect == "snapshot":
        selection["screening_snapshot_id"] = "synthetic-unbound-snapshot"
    elif defect == "policy":
        selection["policy"]["positive_weight"] += 1
    bind_selection_evidence(payload, rebuild_screening=False)
    result = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert result.report is not None and result.report.result.selection is not None
    outcome = result.report.result.selection
    data_failure = defect in {
        "observation-duplicate",
        "observation-missing",
        "industry",
        "signal-missing",
        "signal-unavailable",
        "signal-state",
    }
    assert outcome.disposition == ("DATA_FAILED" if data_failure else "SYSTEM_FAILED")
    assert outcome.members == ()
    assert outcome.ranking == ()
    assert outcome.population.availability_failure == ("DATA" if data_failure else "SYSTEM")
    assert not outcome.population.valid_monthly


def test_curves_and_low_rank_interactions_replay_contributions(migrated_settings: Settings) -> None:
    selection = selection_payload(migrated_settings)["selection"]
    strategy, artifact = selection["screening"]["strategy"], selection["screening"]["artifact"]
    positive, terminal = "synthetic-positive-signal", "synthetic-terminal-signal"
    strategy["signals"][0]["monotone"] = False
    strategy["knots"] = ["0", "50", "100"]
    strategy["interactions"] = [
        {"interaction_id": "synthetic-pair", "left": positive, "right": terminal}
    ]
    for head in (artifact["positive"], artifact["terminal"]):
        head["main_effects"] = {positive: ["0", "4", "1"], terminal: ["0", "0", "0"]}
        head["interactions"] = {
            "synthetic-pair": [
                {
                    "coefficient": "2",
                    "left": ["0", "1", "2"],
                    "right": ["0", "1", "2"],
                }
            ]
        }
    seal(selection)
    audit, failures = replay(selection)
    assert failures == ()
    assert audit[0].positive_contributions[positive] == Decimal(1)
    assert audit[0].positive_contributions["synthetic-pair"] == Decimal(8)
    assert audit[0].positive_score == Decimal(9)
    assert audit[-1].positive_score == 0
    assert audit[0].signals[0].raw == 12
    assert audit[0].signals[0].percentile == 100


def test_industry_reversal_and_legal_minimum_states(migrated_settings: Settings) -> None:
    selection = selection_payload(migrated_settings)["selection"]
    snapshot = selection["screening"]
    feature = snapshot["strategy"]["signals"][0]
    feature.update(population="INDUSTRY", reverse=True, minimum_states=["NON_POSITIVE_EQUITY"])
    snapshot["observations"][0]["signals"][0].update(raw=None, state="NON_POSITIVE_EQUITY")
    seal(selection)
    audit, failures = replay(selection)
    assert failures == ()
    by_id = {row.security_id: row for row in audit}
    assert by_id["XQZ-SELECT-000"].signals[0].percentile == 0
    assert by_id["XQZ-SELECT-006"].signals[0].percentile == 100
    assert by_id["XQZ-SELECT-001"].signals[0].percentile == 0
    assert by_id["XQZ-SELECT-007"].signals[0].percentile == 100


def test_reversed_precise_signals_ignore_ambient_decimal_context(
    migrated_settings: Settings,
) -> None:
    selection = selection_payload(migrated_settings)["selection"]
    snapshot = selection["screening"]
    snapshot["strategy"]["signals"][0].update(population="INDUSTRY", reverse=True)
    snapshot["observations"][0]["signals"][0]["raw"] = "1.00000000000000000000000000001"
    snapshot["observations"][6]["signals"][0]["raw"] = "1.00000000000000000000000000002"
    seal(selection)
    results = []
    for precision in (6, 28, 50):
        with localcontext() as context:
            context.prec = precision
            audit, failures = replay(selection)
            assert failures == ()
            assert audit[0].signals[0].percentile == 100
            assert audit[6].signals[0].percentile == 0
            results.append(audit)
    assert results[0] == results[1] == results[2]


@pytest.mark.parametrize(
    "months",
    [
        ["2043-01", "2043-02"],
        ["2030-01", "2041-02"],
    ],
)
def test_training_window_cannot_select_future_or_discontinuous_months(
    migrated_settings: Settings,
    months: list[str],
) -> None:
    selection = selection_payload(migrated_settings)["selection"]
    artifact = selection["screening"]["artifact"]
    artifact["training_months"] = months
    for member, month in zip(artifact["training_members"], months, strict=True):
        member["month"] = month
    artifact["training_members_sha256"] = digest(artifact["training_members"])
    seal(selection)
    audit, failures = replay(selection)
    assert audit == ()
    assert "SCREENING_TRAINING_PROVENANCE_INVALID" in failures


@pytest.mark.parametrize("field", ["industry_version", "adjustment_version", "semantics"])
def test_data_contract_change_requires_new_strategy(
    migrated_settings: Settings,
    field: str,
) -> None:
    payload = selection_payload(migrated_settings)
    if field == "semantics":
        payload["selection"]["manifest"]["entries"][-1]["semantics_version"] = (
            "synthetic-new-semantics"
        )
    else:
        payload["selection"][field] = "synthetic-new-version"
        bind_selection_evidence(payload, rebuild_screening=False)
    result = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert result.report is not None and result.report.result.selection is not None
    assert result.report.result.selection.disposition == "SYSTEM_FAILED"
    assert "SCREENING_VERSION_MISMATCH" in result.report.result.selection.reasons


@pytest.mark.parametrize(
    "case", ["rolling", "expanding", "calendar-gap", "wrong-month", "immature"]
)
def test_training_calendar_selects_the_prescribed_mature_window(
    migrated_settings: Settings,
    case: str,
) -> None:
    selection = selection_payload(migrated_settings)["selection"]
    strategy, artifact = selection["screening"]["strategy"], selection["screening"]["artifact"]
    strategy["training_start_month"] = "2040-11"
    artifact["training_calendar"][:0] = [
        {
            "month": "2040-11",
            "selection_cutoff_at": "2040-11-30T23:59:59+08:00",
            "calendar": monthly_training_calendar("2040-11"),
        },
        {
            "month": "2040-12",
            "selection_cutoff_at": "2040-12-31T23:59:59+08:00",
            "calendar": monthly_training_calendar("2040-12"),
        },
    ]
    strategy["rolling_mature_months"] = 2
    if case == "expanding":
        strategy["rolling_mature_months"] = 4
        artifact["training_months"][:0] = ["2040-11", "2040-12"]
        artifact["training_members"][:0] = [
            {**artifact["training_members"][0], "month": month} for month in ("2040-11", "2040-12")
        ]
        artifact["training_members_sha256"] = digest(artifact["training_members"])
    elif case == "calendar-gap":
        artifact["training_calendar"].pop(0)
    elif case == "wrong-month":
        artifact["training_calendar"][0]["selection_cutoff_at"] = "2040-12-01T23:59:59+08:00"
    elif case == "immature":
        strategy["label_horizon_months"] = 16
        artifact["training_calendar"].pop()
    seal(selection)
    audit, failures = replay(selection)
    if case in {"rolling", "expanding"}:
        assert failures == ()
        assert len(audit) == 12
    else:
        assert audit == ()
        assert "SCREENING_TRAINING_PROVENANCE_INVALID" in failures


@pytest.mark.parametrize(
    "cutoff",
    [
        "2041-02-28T00:00:00+08:00",
        "2041-02-01T23:59:59+08:00",
    ],
)
def test_training_cutoff_must_be_the_original_monthly_boundary(
    migrated_settings: Settings,
    cutoff: str,
) -> None:
    selection = selection_payload(migrated_settings)["selection"]
    artifact = selection["screening"]["artifact"]
    artifact["training_calendar"][-1]["selection_cutoff_at"] = cutoff
    artifact["training_members"][-1]["label_available_at"] = "2042-05-28T12:00:00+08:00"
    artifact["training_members_sha256"] = digest(artifact["training_members"])
    seal(selection)
    audit, failures = replay(selection)
    assert audit == ()
    assert "SCREENING_TRAINING_PROVENANCE_INVALID" in failures


@pytest.mark.parametrize(
    "case", ["missing", "version", "incomplete", "late-close", "holiday", "utc"]
)
def test_historical_calendar_proves_its_monthly_cutoff(
    migrated_settings: Settings,
    case: str,
) -> None:
    selection = selection_payload(migrated_settings)["selection"]
    record = selection["screening"]["artifact"]["training_calendar"][-1]
    if case == "missing":
        record["calendar"] = None
    elif case == "version":
        record["calendar"]["version_id"] = "synthetic-unregistered-calendar"
    elif case == "incomplete":
        record["calendar"]["days"].pop(0)
    elif case == "late-close":
        record["calendar"]["days"][-1]["close_at"] = "2041-02-28T23:59:59.000001+08:00"
    elif case == "holiday":
        record["calendar"]["days"][-1]["close_at"] = None
        record["selection_cutoff_at"] = "2041-02-27T23:59:59+08:00"
    elif case == "utc":
        record["selection_cutoff_at"] = "2041-02-28T15:59:59Z"
    seal(selection)
    audit, failures = replay(selection)
    if case in {"holiday", "utc"}:
        assert failures == ()
        assert len(audit) == 12
    else:
        assert audit == ()
        assert "SCREENING_TRAINING_PROVENANCE_INVALID" in failures


@pytest.mark.parametrize(
    "defect",
    [
        "none",
        "missing",
        "duplicate",
        "unknown",
        "exploratory",
        "no-proof",
        "preclose",
        "stale",
        "unregistered-alternate",
        "certified-alternate",
        "alternate-stale-authority",
        "alternate-preclose-authority",
    ],
)
def test_selection_field_manifests_and_substitute_reconstruction(
    migrated_settings: Settings,
    defect: str,
) -> None:
    payload = selection_payload(migrated_settings)
    manifest = payload["selection"]["manifest"]
    entry = next(row for row in manifest["entries"] if row["field_family"] == "adjusted_returns")
    if defect == "none":
        payload["selection"]["manifest"] = None
    elif defect == "missing":
        manifest["entries"].remove(entry)
    elif defect == "duplicate":
        manifest["entries"].append(deepcopy(entry))
    elif defect == "unknown":
        entry["field_family"] = "synthetic-unregistered-family"
    elif defect == "exploratory":
        entry["requirement"] = "EXPLORATORY"
    elif defect == "no-proof":
        entry["evidence"] = None
    elif defect == "preclose":
        entry["evidence"]["source_published_at"] = "2042-05-30T09:00:00+08:00"
    elif defect == "stale":
        entry["evidence"]["fact_effective_at"] = "2042-05-29T15:00:00+08:00"
    else:
        authority = deepcopy(entry["evidence"])
        entry["evidence"]["source"] = "fictional-alternate-731"
        entry["evidence"]["authority"] = "CERTIFIED_DELIVERY"
        if defect != "unregistered-alternate":
            entry["substitution"] = substitution(manifest, entry, authority)
        if defect == "alternate-stale-authority":
            authority["fact_effective_at"] = "2042-05-29T15:00:00+08:00"
        elif defect == "alternate-preclose-authority":
            authority["source_published_at"] = "2042-05-30T09:00:00+08:00"
    result = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-30T16:05:00Z")
    )
    assert result.report is not None and result.report.result.selection is not None
    assert result.report.result.selection.disposition == (
        "FROZEN" if defect == "certified-alternate" else "DATA_FAILED"
    )


@pytest.mark.parametrize("defect", ["valid", "inconsistent", "out-of-range", "empty-rank", "curve"])
def test_context_only_enters_frozen_interaction(migrated_settings: Settings, defect: str) -> None:
    selection = selection_payload(migrated_settings)["selection"]
    snapshot = selection["screening"]
    strategy, artifact = snapshot["strategy"], snapshot["artifact"]
    context = strategy["signals"][0]["signal_id"]
    other = strategy["signals"][1]["signal_id"]
    strategy["signals"][0]["population"] = "CONTEXT"
    strategy["interactions"] = [
        {"interaction_id": "synthetic-context-pair", "left": context, "right": other}
    ]
    for row in snapshot["observations"]:
        row["signals"][0]["raw"] = "50"
    for head in (artifact["positive"], artifact["terminal"]):
        head["main_effects"].pop(context)
        head["interactions"] = {
            "synthetic-context-pair": [
                {
                    "coefficient": "1",
                    "left": ["0", "2"],
                    "right": ["0", "2"],
                }
            ]
        }
    if defect == "inconsistent":
        snapshot["observations"][0]["signals"][0]["raw"] = "20"
    elif defect == "out-of-range":
        snapshot["observations"][0]["signals"][0]["raw"] = "101"
    elif defect == "empty-rank":
        artifact["positive"]["interactions"]["synthetic-context-pair"] = []
    elif defect == "curve":
        artifact["positive"]["interactions"]["synthetic-context-pair"][0]["left"] = ["0"]
    seal(selection)
    audit, failures = replay(selection)
    if defect == "valid":
        assert failures == ()
        assert context not in audit[0].positive_contributions
        assert audit[0].positive_contributions["synthetic-context-pair"] == 2
    else:
        assert audit == ()
        assert failures
