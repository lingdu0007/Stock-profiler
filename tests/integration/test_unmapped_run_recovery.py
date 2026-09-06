from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from stock_profiler.adapters.m_agent.frozen_decision_case import execute_frozen_decision_case
from stock_profiler.adapters.persistence.decision_ledger import (
    DECISION_CASE_BUSINESS_OBJECTS,
    DecisionEventCommitError,
    DecisionLedger,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap import decision_cases as bootstrap
from stock_profiler.bootstrap.decision_cases import (
    get_formal_report,
    replay_default_frozen_decision_case,
    run_default_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints import cli
from stock_profiler.modules.decision_cases.domain import (
    FrozenDecisionCase,
    load_frozen_decision_case,
)


@pytest.mark.parametrize("snapshotless_mapping", [False, True])
def test_unmapped_cross_build_run_requires_original_provenance_before_recovery(
    migrated_settings: Settings,
    snapshotless_mapping: bool,
) -> None:
    original_case = load_frozen_decision_case(migrated_settings).legacy_contract_recovery_cases[0]
    runtime = initialize_runtime_storage(migrated_settings)
    original = asyncio.run(execute_frozen_decision_case(original_case, runtime))
    if snapshotless_mapping:
        ledger = DecisionLedger.from_settings(migrated_settings)
        with ledger.serialize_case_execution() as connection:
            ledger.ensure_business_object(connection, original_case)
            connection.execute(DECISION_CASE_BUSINESS_OBJECTS.update().values(case_payload=None))
    upgraded = migrated_settings.model_copy(update={"source_sha": "b" * 40})
    replacement_case = load_frozen_decision_case(upgraded)

    with pytest.raises(DecisionEventCommitError):
        run_default_frozen_decision_case(upgraded)

    assert asyncio.run(runtime.run_store.get_run(replacement_case.framework_run_id)) is None
    assert get_formal_report(replacement_case.report_version_id, upgraded) is None
    recovered = replay_default_frozen_decision_case(
        upgraded, original_case.business_identity, recovery_case=original_case
    )
    assert recovered.framework_run_id == original.run_id
    assert recovered.publication_status == "PUBLISHED"
    assert run_default_frozen_decision_case(upgraded).report == recovered.report


def test_mapped_result_families_can_share_the_same_framework_store(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = run_default_frozen_decision_case(migrated_settings)
    payload = Path("tests/fixtures/synthetic/result-families/result-abstained.json").read_text()
    case = FrozenDecisionCase.model_validate_json(payload)
    case = case.model_copy(
        update={
            "version_bundle": case.version_bundle.model_copy(
                update={
                    "host_source_sha": migrated_settings.source_sha,
                    "host_application_version": migrated_settings.configuration_version,
                }
            )
        }
    )
    monkeypatch.setattr(bootstrap, "load_frozen_decision_case", lambda _: case)
    abstained = run_default_frozen_decision_case(migrated_settings)
    assert abstained.business_result_status == "ABSTAINED"
    assert abstained.framework_run_id != original.framework_run_id
    assert abstained.publication_status == "PUBLISHED"


@pytest.mark.parametrize("invalid_snapshot", ["missing-run", "changed-input"])
def test_explicit_recovery_requires_matching_durable_proof_before_any_host_write(
    migrated_settings: Settings, invalid_snapshot: str
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    if invalid_snapshot == "changed-input":
        case = case.model_copy(update={"input": {**case.input, "unrecognized": True}})
    with pytest.raises(ValueError, match="original"):
        replay_default_frozen_decision_case(
            migrated_settings, case.business_identity, recovery_case=case
        )
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 0,
        "decision_events": 0,
        "reports": 0,
    }


def test_cli_replays_the_original_snapshot_after_a_source_change(
    migrated_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case = load_frozen_decision_case(migrated_settings).legacy_contract_recovery_cases[0]
    original = asyncio.run(
        execute_frozen_decision_case(case, initialize_runtime_storage(migrated_settings))
    )
    snapshot = tmp_path / "original-case.json"
    snapshot.write_text(case.model_dump_json())
    upgraded = migrated_settings.model_copy(update={"source_sha": "b" * 40})
    monkeypatch.setattr(cli, "load_settings", lambda: upgraded)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stock-profiler",
            "decision-case-replay",
            "--business-identity",
            case.business_identity,
            "--recovery-case",
            str(snapshot),
        ],
    )
    cli.main()
    assert original.run_id in capsys.readouterr().out
