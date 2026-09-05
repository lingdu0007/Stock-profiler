from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Literal

import pytest
from sqlalchemy.engine import Connection

from stock_profiler.adapters.m_agent.frozen_decision_case import FrameworkRunResult
from stock_profiler.adapters.persistence.decision_ledger import (
    DecisionEventCommitError,
    DecisionLedger,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    ExternalResult,
    FrozenDecisionCase,
    StageResult,
    load_frozen_decision_case,
)
from stock_profiler.modules.decision_cases.service import (
    correct_default_frozen_decision_case,
    get_formal_report,
    retry_default_frozen_decision_case_notification,
    run_default_frozen_decision_case,
)


def test_successful_case_publishes_one_event_and_report_after_a_framework_run(
    migrated_settings: Settings,
) -> None:
    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_commit_status == "COMMITTED"
    assert outcome.publication_status == "PUBLISHED"
    assert outcome.report is not None
    assert outcome.report.event_id == outcome.decision_event_id
    assert outcome.report.framework_run_id == outcome.framework_run_id
    assert outcome.report.synthetic is True
    assert outcome.report.qualification_scope == "D0_SYNTHETIC_CONTRACT_ONLY"
    assert outcome.report.evidence_clock.validated_at == "2042-05-17T15:18:00Z"
    assert get_formal_report(outcome.report_version_id, migrated_settings) == outcome.report


def test_framework_success_and_host_rejection_remain_distinct_committed_results(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    async def rejected_framework_run(*_: object) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="SUCCEEDED",
            output=(
                '{"key_reasons":["The host rejected the frozen input."],'
                '"outcome_code":"SYNTHETIC_INPUT_REJECTED",'
                '"summary":"Synthetic D0 decision case was rejected by a host gate."}'
            ),
        )

    monkeypatch.setattr(service, "execute_frozen_decision_case", rejected_framework_run)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_commit_status == "COMMITTED"
    assert outcome.publication_status == "PUBLISHED"
    assert outcome.report is not None
    assert outcome.report.result.outcome_code == "SYNTHETIC_INPUT_REJECTED"
    assert [(result.phase, result.status) for result in outcome.stage_results] == [
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        ("HOST_VALIDATION", "REJECTED"),
        ("BUSINESS_COMMIT", "SUCCEEDED"),
        ("PUBLICATION", "SUCCEEDED"),
    ]
    assert outcome.stage_results[1].gate_results[0].gate_id == "FROZEN_RESULT_MATCH"
    assert outcome.stage_results[1].reasons == ("SYNTHETIC_INPUT_REJECTED",)


@pytest.mark.parametrize(
    ("outcome_code", "expected_status", "published"),
    (
        ("SYNTHETIC_RESULT_ABSTAINED", "ABSTAINED", True),
        ("SYNTHETIC_RESULT_EXPIRED", "EXPIRED", True),
        ("SYNTHETIC_RESULT_EXECUTION_BLOCKED", "EXECUTION_BLOCKED", True),
        ("SYNTHETIC_RESULT_PENDING", "PENDING", False),
        ("SYNTHETIC_RESULT_FAILED", "FAILED", False),
    ),
)
def test_host_result_families_keep_their_own_saved_lifecycle(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    outcome_code: str,
    expected_status: str,
    published: bool,
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    async def framework_run(*_: object) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="SUCCEEDED",
            output=json.dumps(
                {
                    "outcome_code": outcome_code,
                    "summary": f"Frozen synthetic result {outcome_code}.",
                    "key_reasons": [outcome_code],
                }
            ),
        )

    monkeypatch.setattr(service, "execute_frozen_decision_case", framework_run)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_result_status == expected_status
    assert outcome.publication_status == ("PUBLISHED" if published else "CLOSED")
    assert outcome.business_commit_status == (
        "COMMITTED" if published else "NOT_ATTEMPTED"
    )
    assert outcome.report is not None if published else outcome.report is None
    assert [(result.phase, result.status) for result in outcome.stage_results[:2]] == [
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        ("HOST_VALIDATION", expected_status),
    ]
    counts = DecisionLedger.from_settings(migrated_settings).counts()
    assert counts == {
        "business_objects": 1,
        "decision_events": 1 if published else 0,
        "reports": 1 if published else 0,
    }


def test_framework_waiting_is_saved_without_inventing_a_host_result(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    async def waiting_framework_run(*_: object) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="WAITING",
            output=None,
        )

    monkeypatch.setattr(service, "execute_frozen_decision_case", waiting_framework_run)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "WAITING"
    assert outcome.business_result_status is None
    assert outcome.business_commit_status == "NOT_ATTEMPTED"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert [(result.phase, result.status) for result in outcome.stage_results] == [
        ("FRAMEWORK_RUN", "WAITING")
    ]
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }


@pytest.mark.parametrize("framework_status", ("REJECTED", "FAILED"))
def test_framework_terminal_failures_remain_framework_results(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    framework_status: Literal["REJECTED", "FAILED"],
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    async def failed_framework_run(*_: object) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status=framework_status,
            output=None,
        )

    monkeypatch.setattr(service, "execute_frozen_decision_case", failed_framework_run)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == framework_status
    assert outcome.business_result_status is None
    assert outcome.business_commit_status == "NOT_ATTEMPTED"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert [(result.phase, result.status) for result in outcome.stage_results] == [
        ("FRAMEWORK_RUN", framework_status)
    ]
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }


def test_invalid_framework_output_contract_is_saved_and_keeps_publication_closed(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    async def invalid_framework_run(*_: object) -> FrameworkRunResult:
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="SUCCEEDED",
            output='{"outcome_code":"SYNTHETIC_REVIEW_COMPLETE"}',
        )

    monkeypatch.setattr(service, "execute_frozen_decision_case", invalid_framework_run)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_result_status == "FAILED"
    assert outcome.business_commit_status == "NOT_ATTEMPTED"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert [(result.phase, result.status) for result in outcome.stage_results] == [
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        ("HOST_VALIDATION", "FAILED"),
    ]
    assert outcome.stage_results[-1].reasons == ("OUTPUT_CONTRACT_INVALID",)
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }


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


def test_uncertain_event_commit_closes_publication_until_the_original_identity_recovers(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
        raise DecisionEventCommitError("synthetic event storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)

        uncertain = run_default_frozen_decision_case(migrated_settings)

    assert uncertain.framework_run_status == "SUCCEEDED"
    assert uncertain.business_result_status == "SUCCEEDED"
    assert uncertain.business_commit_status == "UNKNOWN"
    assert uncertain.publication_status == "CLOSED"
    assert uncertain.report is None
    assert uncertain.stage_results[-1].phase == "BUSINESS_COMMIT"
    assert uncertain.stage_results[-1].status == "UNKNOWN"

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 0, "reports": 0}
    assert [result.status for result in ledger.get_stage_results(uncertain.business_object_id)] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "UNKNOWN",
    ]

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.framework_run_id == uncertain.framework_run_id
    assert recovered.decision_event_id == uncertain.decision_event_id
    assert recovered.report is not None
    assert recovered.publication_status == "PUBLISHED"
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}
    assert [result.status for result in ledger.get_stage_results(recovered.business_object_id)] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "UNKNOWN",
        "SUCCEEDED",
        "SUCCEEDED",
    ]


def test_uncertain_commit_lookup_reuses_the_original_committed_identity(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_commit = DecisionLedger.commit_event

    def commit_then_report_uncertainty(
        self: DecisionLedger,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        framework_run_id: str,
        result: ExternalResult,
        stage_results: tuple[StageResult, ...],
        decision_event_id: str | None = None,
        corrects_event_id: str | None = None,
    ) -> DecisionEventFact:
        original_commit(
            self,
            connection,
            case=case,
            framework_run_id=framework_run_id,
            result=result,
            stage_results=stage_results,
            decision_event_id=decision_event_id,
            corrects_event_id=corrects_event_id,
        )
        raise DecisionEventCommitError("synthetic acknowledgement loss")

    monkeypatch.setattr(DecisionLedger, "commit_event", commit_then_report_uncertainty)

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_commit_status == "COMMITTED"
    assert outcome.publication_status == "PUBLISHED"
    assert outcome.report is not None
    assert outcome.framework_run_id == load_frozen_decision_case(migrated_settings).framework_run_id
    assert outcome.decision_event_id == load_frozen_decision_case(
        migrated_settings
    ).decision_event_id
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


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
        unpublished = run_default_frozen_decision_case(migrated_settings)

    assert unpublished.framework_run_status == "SUCCEEDED"
    assert unpublished.business_result_status == "SUCCEEDED"
    assert unpublished.business_commit_status == "COMMITTED"
    assert unpublished.publication_status == "CLOSED"
    assert unpublished.report is None
    assert unpublished.stage_results[-1].phase == "PUBLICATION"
    assert unpublished.stage_results[-1].status == "FAILED"
    assert unpublished.stage_results[-1].reasons == ("PUBLICATION_STORAGE_FAILED",)

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 0}

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.publication_status == "PUBLISHED"
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}
    assert [result.status for result in ledger.get_stage_results(recovered.business_object_id)] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
        "SUCCEEDED",
    ]


def test_notification_failure_and_retry_preserve_the_original_published_report(
    migrated_settings: Settings,
) -> None:
    original_execution = run_default_frozen_decision_case(migrated_settings)
    assert original_execution.report is not None
    case = load_frozen_decision_case(migrated_settings)

    def failing_notification(_: object) -> None:
        raise RuntimeError("synthetic notification transport failure")

    failed = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        failing_notification,
    )
    repeated_failure = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        failing_notification,
    )
    recovered = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        lambda _: None,
    )

    assert failed.status == "FAILED"
    assert failed.report_version_id == original_execution.report.report_version_id
    assert repeated_failure.status == "FAILED"
    assert repeated_failure.notification_attempt_id != failed.notification_attempt_id
    assert recovered.status == "SUCCEEDED"
    assert recovered.report_version_id == original_execution.report.report_version_id
    assert get_formal_report(original_execution.report.report_version_id, migrated_settings) == (
        original_execution.report
    )
    ledger = DecisionLedger.from_settings(migrated_settings)
    notification_attempts = ledger.get_notification_attempts(case.report_version_id)
    assert [attempt.status for attempt in notification_attempts] == [
        "FAILED",
        "FAILED",
        "SUCCEEDED",
    ]
    assert [
        stage.status
        for stage in ledger.get_stage_results(case.business_object_id)
        if stage.phase == "NOTIFICATION"
    ] == ["FAILED", "FAILED", "SUCCEEDED"]
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}


def test_correction_appends_a_new_report_that_references_the_original_event(
    migrated_settings: Settings,
) -> None:
    original_execution = run_default_frozen_decision_case(migrated_settings)
    assert original_execution.report is not None
    original_report = original_execution.report
    case = load_frozen_decision_case(migrated_settings)

    correction = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
    )
    replayed_correction = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
    )

    assert replayed_correction == correction
    assert correction.original_event_id == original_report.event_id
    assert correction.report.event_id != original_report.event_id
    assert correction.report.report_version_id != original_report.report_version_id
    assert correction.report.corrects_event_id == original_report.event_id
    assert correction.report.framework_run_id == original_report.framework_run_id
    assert correction.report.knowledge_cutoff == original_report.knowledge_cutoff
    assert correction.report.evidence_clock == original_report.evidence_clock
    assert (
        get_formal_report(original_report.report_version_id, migrated_settings) == original_report
    )
    assert (
        get_formal_report(correction.report.report_version_id, migrated_settings)
        == correction.report
    )
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 2,
        "reports": 2,
    }


def test_read_failure_does_not_remove_the_published_report(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_execution = run_default_frozen_decision_case(migrated_settings)
    assert original_execution.report is not None
    original_report = original_execution.report

    with monkeypatch.context() as patch:
        patch.setattr(
            DecisionLedger,
            "get_formal_report",
            lambda *_: (_ for _ in ()).throw(RuntimeError("synthetic read failure")),
        )
        with pytest.raises(RuntimeError, match="synthetic read failure"):
            get_formal_report(original_report.report_version_id, migrated_settings)

    assert (
        get_formal_report(original_report.report_version_id, migrated_settings) == original_report
    )
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_snapshot_and_definition_mutations_fail_before_an_official_event_is_published(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    incomplete_snapshot = case.model_copy(update={"input": {**case.input, "evidence": []}})
    monkeypatch.setattr(service, "load_frozen_decision_case", lambda _: incomplete_snapshot)

    incomplete = run_default_frozen_decision_case(migrated_settings)

    assert incomplete.business_result_status == "FAILED"
    assert incomplete.business_commit_status == "NOT_ATTEMPTED"
    assert incomplete.publication_status == "CLOSED"
    assert incomplete.report is None
    assert incomplete.stage_results[-1].reasons == ("UNSUPPORTED_SYNTHETIC_RESULT",)
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
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

    with pytest.raises(DecisionEventCommitError, match="business identity maps"):
        run_default_frozen_decision_case(migrated_settings)

    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 0,
        "reports": 0,
    }

def test_reading_a_published_report_does_not_rebuild_it_from_current_code(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = run_default_frozen_decision_case(migrated_settings).report
    assert original is not None
    monkeypatch.setattr(
        DecisionEventFact,
        "formal_report",
        lambda *_: (_ for _ in ()).throw(AssertionError("report must be read from storage")),
    )

    assert get_formal_report(original.report_version_id, migrated_settings) == original
