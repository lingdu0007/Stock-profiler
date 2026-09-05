from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from stock_profiler.adapters.persistence.decision_ledger import (
    DecisionEventCommitError,
    DecisionLedger,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    load_frozen_decision_case,
)
from stock_profiler.modules.decision_cases.service import (
    get_formal_report,
    run_default_frozen_decision_case,
)


def test_successful_case_publishes_one_event_and_report_after_a_framework_run(
    migrated_settings: Settings,
) -> None:
    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_commit_status == "COMMITTED"
    assert outcome.publication_status == "PUBLISHED"
    assert outcome.report.event_id == outcome.decision_event_id
    assert outcome.report.framework_run_id == outcome.framework_run_id
    assert outcome.report.synthetic is True
    assert outcome.report.qualification_scope == "D0_SYNTHETIC_CONTRACT_ONLY"
    assert outcome.report.evidence_clock.validated_at == "2042-05-17T15:18:00Z"
    assert get_formal_report(outcome.report_version_id, migrated_settings) == outcome.report


def test_replay_reuses_the_original_business_object_framework_run_event_and_report(
    migrated_settings: Settings,
) -> None:
    first = run_default_frozen_decision_case(migrated_settings)
    second = run_default_frozen_decision_case(migrated_settings)
    ledger = DecisionLedger.from_settings(migrated_settings)

    assert second == first
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}


def test_concurrent_replay_resolves_to_the_one_committed_identity_set(
    migrated_settings: Settings,
) -> None:
    start_together = Barrier(2)

    def replay() -> object:
        start_together.wait(timeout=5)
        return run_default_frozen_decision_case(migrated_settings)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(replay)
        second_future = executor.submit(replay)
        first = first_future.result(timeout=10)
        second = second_future.result(timeout=10)

    assert first == second
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_event_commit_failure_never_leaves_a_readable_formal_report(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
        raise DecisionEventCommitError("synthetic event storage failure")

    monkeypatch.setattr(DecisionLedger, "commit_event", fail_commit)

    with pytest.raises(DecisionEventCommitError):
        run_default_frozen_decision_case(migrated_settings)

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 0, "decision_events": 0, "reports": 0}


def test_report_projection_failure_preserves_the_committed_event_but_never_publishes_it(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(
            DecisionLedger,
            "publish_report",
            lambda self, _connection, _fact: (_ for _ in ()).throw(
                DecisionEventCommitError("synthetic projection storage failure")
            ),
        )
        with pytest.raises(DecisionEventCommitError, match="projection storage failure"):
            run_default_frozen_decision_case(migrated_settings)

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 0}

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.publication_status == "PUBLISHED"
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}


def test_snapshot_and_definition_mutations_fail_before_an_official_event_is_published(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    incomplete_snapshot = case.model_copy(update={"input": {**case.input, "evidence": []}})
    monkeypatch.setattr(service, "load_frozen_decision_case", lambda _: incomplete_snapshot)

    with pytest.raises(DecisionEventCommitError, match="host validation rejected"):
        run_default_frozen_decision_case(migrated_settings)

    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 0,
        "decision_events": 0,
        "reports": 0,
    }

    altered_evidence = [*case.input["evidence"]]
    altered_evidence[0] = {
        **altered_evidence[0],
        "statement": "A changed statement with the same synthetic evidence identifier.",
    }
    altered_snapshot = case.model_copy(
        update={"input": {**case.input, "evidence": altered_evidence}}
    )
    monkeypatch.setattr(service, "load_frozen_decision_case", lambda _: altered_snapshot)

    with pytest.raises(DecisionEventCommitError, match="host validation rejected"):
        run_default_frozen_decision_case(migrated_settings)

    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 0,
        "decision_events": 0,
        "reports": 0,
    }

    changed_definition = case.model_copy(
        update={
            "agent_definition": case.agent_definition.model_copy(
                update={"instructions": "Return a changed response."}
            )
        }
    )
    monkeypatch.setattr(service, "load_frozen_decision_case", lambda _: changed_definition)

    with pytest.raises(ValueError, match="frozen AgentDefinition"):
        run_default_frozen_decision_case(migrated_settings)


def test_reading_a_published_report_does_not_rebuild_it_from_current_code(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = run_default_frozen_decision_case(migrated_settings).report
    monkeypatch.setattr(
        DecisionEventFact,
        "formal_report",
        lambda *_: (_ for _ in ()).throw(AssertionError("report must be read from storage")),
    )

    assert get_formal_report(original.report_version_id, migrated_settings) == original
