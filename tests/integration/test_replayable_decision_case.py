from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from stock_profiler.adapters.persistence.decision_ledger import (
    DecisionEventCommitError,
    DecisionLedger,
)
from stock_profiler.bootstrap.settings import Settings
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

    monkeypatch.setattr(DecisionLedger, "commit_event_and_report", fail_commit)

    with pytest.raises(DecisionEventCommitError):
        run_default_frozen_decision_case(migrated_settings)

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 0, "decision_events": 0, "reports": 0}
