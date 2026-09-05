from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Literal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

from stock_profiler.adapters.m_agent import frozen_decision_case as frozen_adapter
from stock_profiler.adapters.m_agent.frozen_decision_case import FrameworkRunResult
from stock_profiler.adapters.persistence.decision_ledger import (
    DecisionEventCommitError,
    DecisionEventCommitUncertainError,
    DecisionLedger,
)
from stock_profiler.adapters.persistence.runtime_ownership import RuntimeStorage
from stock_profiler.bootstrap.settings import Settings, load_settings
from stock_profiler.modules.decision_cases import service
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    ExternalResult,
    FormalReport,
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

ROOT = Path(__file__).resolve().parents[2]


def test_successful_case_publishes_one_event_and_report_after_a_framework_run(
    migrated_settings: Settings,
) -> None:
    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_result_status == "SUCCEEDED"
    assert outcome.business_lifecycle_status is None
    assert outcome.business_commit_status == "COMMITTED"
    assert outcome.publication_status == "PUBLISHED"
    assert outcome.report is not None
    assert outcome.report.event_id == outcome.decision_event_id
    assert outcome.report.framework_run_id == outcome.framework_run_id
    assert outcome.report.synthetic is True
    assert outcome.report.qualification_scope == "D0_SYNTHETIC_CONTRACT_ONLY"
    assert outcome.report.evidence_clock.validated_at == "2042-05-17T15:18:00Z"
    assert outcome.stage_results[1].reasons == (
        "All required fictional evidence records are present.",
        "The output is D0 synthetic evidence and is not a recommendation.",
    )
    assert get_formal_report(outcome.report_version_id, migrated_settings) == outcome.report


def test_formal_report_projection_fixture_matches_the_frozen_runtime(
    migrated_settings: Settings,
) -> None:
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "synthetic" / "formal_report_projection.json").read_text(
            encoding="utf-8"
        )
    )
    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.report is not None
    assert fixture["report"] == outcome.report.model_dump(mode="json")


def test_framework_success_and_host_rejection_remain_distinct_committed_results(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        frozen_adapter,
        "_deterministic_model_response",
        lambda _: (
            '{"key_reasons":["The host rejected the frozen input."],'
            '"outcome_code":"SYNTHETIC_INPUT_REJECTED",'
            '"summary":"Synthetic D0 decision case was rejected by a host gate."}'
        ),
    )

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
    assert outcome.stage_results[1].reasons == ("The host rejected the frozen input.",)


@pytest.mark.parametrize(
    ("outcome_code", "expected_business_status", "expected_lifecycle_status", "published"),
    (
        ("SYNTHETIC_RESULT_ABSTAINED", "ABSTAINED", None, True),
        ("SYNTHETIC_RESULT_EXPIRED", None, "EXPIRED", False),
        ("SYNTHETIC_RESULT_EXECUTION_BLOCKED", None, "EXECUTION_BLOCKED", False),
        ("SYNTHETIC_RESULT_UNKNOWN", None, "UNKNOWN", False),
        ("SYNTHETIC_RESULT_PENDING", None, "PENDING", False),
        ("SYNTHETIC_RESULT_FAILED", "FAILED", None, False),
    ),
)
def test_host_result_families_keep_their_own_saved_lifecycle(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    outcome_code: str,
    expected_business_status: str | None,
    expected_lifecycle_status: str | None,
    published: bool,
) -> None:
    case = load_frozen_decision_case(migrated_settings)

    monkeypatch.setattr(
        frozen_adapter,
        "_deterministic_model_response",
        lambda _: json.dumps(
            {
                "outcome_code": outcome_code,
                "summary": f"Frozen synthetic result {outcome_code}.",
                "key_reasons": [f"Reason for {outcome_code}."],
            }
        ),
    )

    outcome = run_default_frozen_decision_case(migrated_settings)
    replayed = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "SUCCEEDED"
    assert outcome.business_result_status == expected_business_status
    assert outcome.business_lifecycle_status == expected_lifecycle_status
    assert outcome.publication_status == ("PUBLISHED" if published else "CLOSED")
    assert outcome.business_commit_status == ("COMMITTED" if published else "NOT_ATTEMPTED")
    assert outcome.report is not None if published else outcome.report is None
    assert [(result.phase, result.status) for result in outcome.stage_results[:2]] == [
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        (
            "HOST_VALIDATION",
            expected_business_status or expected_lifecycle_status,
        ),
    ]
    assert outcome.stage_results[1].reasons == (f"Reason for {outcome_code}.",)
    assert replayed == outcome
    assert replayed.framework_run_id == case.framework_run_id
    assert replayed.decision_event_id == case.decision_event_id
    assert replayed.report_version_id == case.report_version_id
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
    assert outcome.business_lifecycle_status is None
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
    assert outcome.business_lifecycle_status is None
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
    assert outcome.business_lifecycle_status is None
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


def test_real_m_agent_output_contract_failure_keeps_its_explicit_reason(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        frozen_adapter,
        "_deterministic_model_response",
        lambda _: '{"outcome_code":"SYNTHETIC_REVIEW_COMPLETE"}',
    )

    outcome = run_default_frozen_decision_case(migrated_settings)

    assert outcome.framework_run_status == "FAILED"
    assert outcome.business_result_status is None
    assert outcome.business_lifecycle_status is None
    assert outcome.business_commit_status == "NOT_ATTEMPTED"
    assert outcome.publication_status == "CLOSED"
    assert outcome.report is None
    assert [(stage.phase, stage.status) for stage in outcome.stage_results] == [
        ("FRAMEWORK_RUN", "FAILED")
    ]
    assert outcome.stage_results[0].reasons == ("MODEL_CONTRACT_VIOLATION",)


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


def test_definite_event_commit_failure_closes_publication_until_the_original_identity_recovers(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
        raise DecisionEventCommitError("synthetic event storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)

        failed = run_default_frozen_decision_case(migrated_settings)

    assert failed.framework_run_status == "SUCCEEDED"
    assert failed.business_result_status == "SUCCEEDED"
    assert failed.business_lifecycle_status is None
    assert failed.business_commit_status == "FAILED"
    assert failed.publication_status == "CLOSED"
    assert failed.report is None
    assert failed.stage_results[-1].phase == "BUSINESS_COMMIT"
    assert failed.stage_results[-1].status == "FAILED"
    assert failed.stage_results[-1].gate_results[0].status == "FAILED"
    assert failed.stage_results[-1].reasons == ("COMMIT_STORAGE_FAILED",)

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 0, "reports": 0}
    assert [result.status for result in ledger.get_stage_results(failed.business_object_id)] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
    ]

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.framework_run_id == failed.framework_run_id
    assert recovered.decision_event_id == failed.decision_event_id
    assert recovered.report is not None
    assert recovered.publication_status == "PUBLISHED"
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 1}
    assert [result.status for result in ledger.get_stage_results(recovered.business_object_id)] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "FAILED",
        "SUCCEEDED",
        "SUCCEEDED",
    ]


def test_cross_build_commit_recovery_resumes_the_original_m_agent_run(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
        raise DecisionEventCommitError("synthetic event storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)
        failed = run_default_frozen_decision_case(migrated_settings)

    upgraded_settings = migrated_settings.model_copy(
        update={
            "configuration_version": "0.1.1.dev0",
            "source_sha": "b" * 40,
        }
    )
    recovered = run_default_frozen_decision_case(upgraded_settings)

    assert failed.publication_status == "CLOSED"
    assert recovered.publication_status == "PUBLISHED"
    assert recovered.business_object_id == failed.business_object_id
    assert recovered.framework_run_id == failed.framework_run_id
    assert recovered.report is not None
    assert recovered.report.framework_run_id == failed.framework_run_id
    assert DecisionLedger.from_settings(upgraded_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_cross_build_worker_interruption_resumes_the_original_m_agent_run(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_execute = frozen_adapter.execute_frozen_decision_case

    async def interrupt_after_framework_checkpoint(
        case: FrozenDecisionCase, runtime: RuntimeStorage
    ) -> FrameworkRunResult:
        await original_execute(case, runtime)
        raise RuntimeError("synthetic worker interruption after framework checkpoint")

    with monkeypatch.context() as patch:
        patch.setattr(service, "execute_frozen_decision_case", interrupt_after_framework_checkpoint)
        with pytest.raises(RuntimeError, match="worker interruption"):
            run_default_frozen_decision_case(migrated_settings)

    original_case = load_frozen_decision_case(migrated_settings)
    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 0, "reports": 0}

    upgraded_settings = migrated_settings.model_copy(
        update={
            "configuration_version": "0.1.1.dev0",
            "source_sha": "b" * 40,
        }
    )
    recovered = run_default_frozen_decision_case(upgraded_settings)

    assert recovered.business_object_id == original_case.business_object_id
    assert recovered.framework_run_id == original_case.framework_run_id
    assert recovered.decision_event_id == original_case.decision_event_id
    assert recovered.report_version_id == original_case.report_version_id
    assert recovered.publication_status == "PUBLISHED"


def test_repeated_business_commit_failures_append_distinct_stage_records(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
        raise DecisionEventCommitError("synthetic event storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)
        first = run_default_frozen_decision_case(migrated_settings)
        second = run_default_frozen_decision_case(migrated_settings)

    assert first.publication_status == "CLOSED"
    assert second.publication_status == "CLOSED"
    assert [
        result.status
        for result in DecisionLedger.from_settings(migrated_settings).get_stage_results(
            first.business_object_id
        )
    ] == ["SUCCEEDED", "SUCCEEDED", "FAILED", "FAILED"]


def test_uncertain_event_commit_closes_publication_until_the_original_identity_recovers(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_commit(self: DecisionLedger, _connection: object, **_: object) -> None:
        raise DecisionEventCommitUncertainError("synthetic uncertain commit")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)

        uncertain = run_default_frozen_decision_case(migrated_settings)

    assert uncertain.framework_run_status == "SUCCEEDED"
    assert uncertain.business_result_status == "SUCCEEDED"
    assert uncertain.business_lifecycle_status is None
    assert uncertain.business_commit_status == "UNKNOWN"
    assert uncertain.publication_status == "CLOSED"
    assert uncertain.report is None
    assert uncertain.stage_results[-1].phase == "BUSINESS_COMMIT"
    assert uncertain.stage_results[-1].status == "UNKNOWN"
    assert uncertain.stage_results[-1].gate_results[0].status == "UNKNOWN"

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
    assert (
        outcome.decision_event_id == load_frozen_decision_case(migrated_settings).decision_event_id
    )
    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 1,
        "decision_events": 1,
        "reports": 1,
    }


def test_report_projection_failure_preserves_the_committed_event_but_never_publishes_it(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_publish = DecisionLedger.publish_report

    def write_report_then_lose_confirmation(
        self: DecisionLedger,
        connection: Connection,
        fact: DecisionEventFact,
    ) -> FormalReport:
        original_publish(self, connection, fact)
        raise DecisionEventCommitError("synthetic projection acknowledgement loss")

    with monkeypatch.context() as patch:
        patch.setattr(
            DecisionLedger,
            "publish_report",
            write_report_then_lose_confirmation,
        )
        unpublished = run_default_frozen_decision_case(migrated_settings)

    assert unpublished.framework_run_status == "SUCCEEDED"
    assert unpublished.business_result_status == "SUCCEEDED"
    assert unpublished.business_lifecycle_status is None
    assert unpublished.business_commit_status == "COMMITTED"
    assert unpublished.publication_status == "CLOSED"
    assert unpublished.report is None
    assert unpublished.stage_results[-1].phase == "PUBLICATION"
    assert unpublished.stage_results[-1].status == "FAILED"
    assert unpublished.stage_results[-1].reasons == ("PUBLICATION_STORAGE_FAILED",)

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 0}
    assert get_formal_report(unpublished.report_version_id, migrated_settings) is None

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


def test_cross_build_publication_failure_keeps_the_committed_fact_identity(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_publication(
        self: DecisionLedger,
        _connection: Connection,
        _fact: DecisionEventFact,
    ) -> FormalReport:
        raise DecisionEventCommitError("synthetic report storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "publish_report", fail_publication)
        initial = run_default_frozen_decision_case(migrated_settings)

    upgraded_settings = migrated_settings.model_copy(update={"source_sha": "b" * 40})
    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "publish_report", fail_publication)
        recovered = run_default_frozen_decision_case(upgraded_settings)

    assert initial.publication_status == "CLOSED"
    assert recovered.publication_status == "CLOSED"
    assert recovered.business_object_id == initial.business_object_id
    assert recovered.framework_run_id == initial.framework_run_id
    assert recovered.decision_event_id == initial.decision_event_id
    assert recovered.report_version_id == initial.report_version_id
    engine = create_engine(upgraded_settings.app_database_url)
    with engine.connect() as connection:
        framework_run_ids = (
            connection.execute(
                text(
                    """
                SELECT DISTINCT framework_run_id
                FROM decision_stage_events
                WHERE decision_event_id = :decision_event_id
                """
                ),
                {"decision_event_id": initial.decision_event_id},
            )
            .scalars()
            .all()
        )
    assert framework_run_ids == [initial.framework_run_id]


def test_notification_failure_and_retry_preserve_the_original_published_report(
    migrated_settings: Settings,
) -> None:
    original_execution = run_default_frozen_decision_case(migrated_settings)
    assert original_execution.report is not None
    case = load_frozen_decision_case(migrated_settings)

    failed = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "FAILED",
    )
    repeated_failure = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "FAILED",
    )
    recovered = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "SUCCEEDED",
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


def test_cross_build_notification_and_correction_reuse_the_original_published_identity(
    migrated_settings: Settings,
) -> None:
    initial = run_default_frozen_decision_case(migrated_settings)
    assert initial.report is not None
    case = load_frozen_decision_case(migrated_settings)
    initial_notification = retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "FAILED",
    )
    upgraded_settings = migrated_settings.model_copy(update={"source_sha": "b" * 40})

    replayed = run_default_frozen_decision_case(upgraded_settings)
    retried_notification = retry_default_frozen_decision_case_notification(
        upgraded_settings,
        case.business_identity,
        "SUCCEEDED",
    )
    initial_correction = correct_default_frozen_decision_case(
        upgraded_settings,
        case.business_identity,
    )
    replayed_correction = correct_default_frozen_decision_case(
        migrated_settings,
        case.business_identity,
    )

    assert replayed.report == initial.report
    assert replayed.framework_run_id == initial.framework_run_id
    assert replayed.decision_event_id == initial.decision_event_id
    assert replayed.report_version_id == initial.report_version_id
    assert retried_notification.report_version_id == initial.report_version_id
    assert retried_notification.event_id == initial.decision_event_id
    assert (
        retried_notification.notification_attempt_id != initial_notification.notification_attempt_id
    )
    assert replayed_correction == initial_correction
    assert initial_correction.report.framework_run_id == initial.framework_run_id
    assert initial_correction.report.event_id == case.correction_event_id(initial.decision_event_id)
    assert initial_correction.report.report_version_id == case.correction_report_version_id(
        initial.decision_event_id
    )


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
    assert correction.report.generated_at != original_report.generated_at
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


def test_stage_result_migration_refuses_to_drop_append_only_history(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0003_decision_stage_events")
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_stage_events (
                    stage_event_id,
                    business_object_id,
                    framework_run_id,
                    decision_event_id,
                    stage_payload,
                    recorded_at
                ) VALUES (
                    'stage-event-synthetic',
                    'business-object-synthetic',
                    'framework-run-synthetic',
                    NULL,
                    '{}',
                    '2042-05-17T16:01:00Z'
                )
                """
            )
        )

    with pytest.raises(RuntimeError, match="append-only stage results"):
        command.downgrade(config, "0002_decision_case_ledger")

    load_settings.cache_clear()


def test_upgrade_of_a_populated_0002_ledger_preserves_replayable_facts_and_reports(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_settings = settings.model_copy(update={"source_sha": "b" * 40})
    case = load_frozen_decision_case(legacy_settings)
    monkeypatch.setenv("STOCK_PROFILER_PROCESS_ROLE", "migrate")
    load_settings.cache_clear()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.app_database_url)
    command.upgrade(config, "0002_decision_case_ledger")

    legacy_event_payload = {
        "decision_event_id": case.decision_event_id,
        "business_object_id": case.business_object_id,
        "framework_run_id": case.framework_run_id,
        "case": case.model_dump(mode="json"),
        "result": case.expected_external_result.model_dump(mode="json"),
        "validation_status": "PASSED",
        "committed_at": case.report_generated_at,
    }
    legacy_report_payload = {
        "report_version_id": case.report_version_id,
        "event_id": case.decision_event_id,
        "business_object_id": case.business_object_id,
        "framework_run_id": case.framework_run_id,
        "case_id": case.case_id,
        "synthetic": True,
        "qualification_scope": case.qualification_scope,
        "generated_at": case.report_generated_at,
        "knowledge_cutoff": case.knowledge_cutoff,
        "evidence_clock": case.evidence_clock.model_dump(mode="json"),
        "version_bundle": case.version_bundle.model_dump(mode="json"),
        "result": case.expected_external_result.model_dump(mode="json"),
    }
    legacy_event_payload_json = json.dumps(
        legacy_event_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    legacy_report_payload_json = json.dumps(
        legacy_report_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    engine = create_engine(settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO decision_case_business_objects (
                    business_object_id,
                    case_id,
                    frozen_input_fingerprint,
                    framework_run_id,
                    created_at
                ) VALUES (
                    :business_object_id,
                    :case_id,
                    :frozen_input_fingerprint,
                    :framework_run_id,
                    :created_at
                )
                """
            ),
            {
                "business_object_id": case.business_object_id,
                "case_id": case.case_id,
                "frozen_input_fingerprint": case.frozen_input_fingerprint,
                "framework_run_id": case.framework_run_id,
                "created_at": case.report_generated_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO decision_events (
                    decision_event_id,
                    business_object_id,
                    framework_run_id,
                    event_payload,
                    committed_at
                ) VALUES (
                    :decision_event_id,
                    :business_object_id,
                    :framework_run_id,
                    :event_payload,
                    :committed_at
                )
                """
            ),
            {
                "decision_event_id": case.decision_event_id,
                "business_object_id": case.business_object_id,
                "framework_run_id": case.framework_run_id,
                "event_payload": legacy_event_payload_json,
                "committed_at": case.report_generated_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO formal_reports (
                    report_version_id,
                    decision_event_id,
                    report_payload,
                    generated_at
                ) VALUES (
                    :report_version_id,
                    :decision_event_id,
                    :report_payload,
                    :generated_at
                )
                """
            ),
            {
                "report_version_id": case.report_version_id,
                "decision_event_id": case.decision_event_id,
                "report_payload": legacy_report_payload_json,
                "generated_at": case.report_generated_at,
            },
        )

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT event_payload FROM decision_events "
                    "WHERE decision_event_id = :decision_event_id"
                ),
                {"decision_event_id": case.decision_event_id},
            ).scalar_one()
            == legacy_event_payload_json
        )
        assert (
            connection.execute(
                text(
                    "SELECT report_payload FROM formal_reports "
                    "WHERE report_version_id = :report_version_id"
                ),
                {"report_version_id": case.report_version_id},
            ).scalar_one()
            == legacy_report_payload_json
        )

    ledger = DecisionLedger.from_settings(settings)
    report = ledger.get_formal_report(case.report_version_id)

    assert report is not None
    assert [(stage.phase, stage.status) for stage in report.stage_results] == [
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        ("HOST_VALIDATION", "SUCCEEDED"),
        ("BUSINESS_COMMIT", "SUCCEEDED"),
        ("PUBLICATION", "SUCCEEDED"),
    ]
    assert [
        (stage.phase, stage.status) for stage in ledger.get_stage_results(case.business_object_id)
    ] == [
        ("FRAMEWORK_RUN", "SUCCEEDED"),
        ("HOST_VALIDATION", "SUCCEEDED"),
        ("BUSINESS_COMMIT", "SUCCEEDED"),
        ("PUBLICATION", "SUCCEEDED"),
    ]
    replayed = run_default_frozen_decision_case(settings)
    assert replayed.report == report
    assert replayed.framework_run_id == case.framework_run_id
    assert replayed.decision_event_id == case.decision_event_id
    assert replayed.report_version_id == case.report_version_id
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM formal_reports WHERE report_version_id = :report_version_id"),
            {"report_version_id": case.report_version_id},
        )

    recovered_without_projection = run_default_frozen_decision_case(settings)
    assert recovered_without_projection.report == report
    assert recovered_without_projection.framework_run_id == case.framework_run_id
    assert recovered_without_projection.decision_event_id == case.decision_event_id
    assert recovered_without_projection.report_version_id == case.report_version_id

    load_settings.cache_clear()


def test_notification_migration_refuses_to_drop_append_only_history(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    case = load_frozen_decision_case(migrated_settings)
    retry_default_frozen_decision_case_notification(
        migrated_settings,
        case.business_identity,
        "FAILED",
    )
    engine = create_engine(migrated_settings.app_database_url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE decision_case_business_objects SET case_payload = NULL"))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", migrated_settings.app_database_url)

    with pytest.raises(RuntimeError, match="append-only notification attempts"):
        command.downgrade(config, "0003_decision_stage_events")

    assert execution.report is not None


def test_frozen_case_snapshot_migration_refuses_to_drop_recovery_state(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", migrated_settings.app_database_url)

    with pytest.raises(RuntimeError, match="frozen case snapshots"):
        command.downgrade(config, "0004_corrections_and_notification_attempts")

    assert execution.report is not None


def test_recovery_backfills_event_stage_results_before_publication(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_record = DecisionLedger.record_stage_result

    def crash_after_event_commit(
        self: DecisionLedger,
        connection: Connection,
        *,
        case: FrozenDecisionCase,
        stage_result: StageResult,
        decision_event_id: str | None = None,
        stage_event_id: str | None = None,
        append: bool = False,
    ) -> None:
        if stage_result.phase == "BUSINESS_COMMIT" and decision_event_id is not None:
            raise RuntimeError("synthetic crash after event commit")
        original_record(
            self,
            connection,
            case=case,
            stage_result=stage_result,
            decision_event_id=decision_event_id,
            stage_event_id=stage_event_id,
            append=append,
        )

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "record_stage_result", crash_after_event_commit)
        with pytest.raises(RuntimeError, match="synthetic crash after event commit"):
            run_default_frozen_decision_case(migrated_settings)

    ledger = DecisionLedger.from_settings(migrated_settings)
    assert ledger.counts() == {"business_objects": 1, "decision_events": 1, "reports": 0}

    recovered = run_default_frozen_decision_case(migrated_settings)

    assert recovered.publication_status == "PUBLISHED"
    assert [stage.status for stage in ledger.get_stage_results(recovered.business_object_id)] == [
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
        "SUCCEEDED",
    ]


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
