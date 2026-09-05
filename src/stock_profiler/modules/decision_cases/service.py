"""Host-owned frozen decision journey: Run evidence, validate, commit, then publish."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock

from pydantic import ValidationError
from sqlalchemy.engine import Connection

from stock_profiler.adapters.m_agent.frozen_decision_case import (
    FrameworkRunResult,
    FrameworkRunTransition,
    execute_frozen_decision_case,
)
from stock_profiler.adapters.persistence.decision_ledger import (
    DecisionEventCommitError,
    DecisionEventCommitUncertainError,
    DecisionLedger,
    FormalReportCommitUncertainError,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    FROZEN_CORRECTION_GENERATED_AT,
    FROZEN_REPORT_PROJECTION_CONTRACT_VERSION,
    BusinessCommitStatus,
    BusinessLifecycle,
    BusinessResultStatus,
    DecisionCaseCorrection,
    DecisionCaseExecution,
    DecisionEventFact,
    ExternalResult,
    FormalReport,
    FrameworkRunStatus,
    FrozenDecisionCase,
    GateResult,
    NotificationAttempt,
    NotificationAttemptStatus,
    StageResult,
    business_lifecycle_from_stage,
    business_outcome_result,
    business_result_status_from_stage,
    framework_run_status_from_stage,
    host_validation_result,
    is_committable_host_outcome,
    load_frozen_decision_case,
)

_FRAMEWORK_EXECUTION_LOCKS: dict[str, Lock] = {}
_FRAMEWORK_EXECUTION_LOCKS_GUARD = Lock()


def run_default_frozen_decision_case(settings: Settings) -> DecisionCaseExecution:
    """Run the published public fixture through the same host module used by every entrypoint."""
    return _run_frozen_decision_case(settings)


def replay_default_frozen_decision_case(
    settings: Settings, business_identity: str
) -> DecisionCaseExecution:
    """Replay only when the caller names the fixture's immutable business identity."""
    case = load_frozen_decision_case(settings)
    if business_identity != case.business_identity:
        raise ValueError("unknown frozen decision-case business identity")
    return _run_frozen_decision_case(settings)


def retry_default_frozen_decision_case_notification(
    settings: Settings,
    business_identity: str,
    status: NotificationAttemptStatus,
) -> NotificationAttempt:
    """Record one recoverable synthetic notification outcome for an existing report."""
    case = load_frozen_decision_case(settings)
    if business_identity != case.business_identity:
        raise ValueError("unknown frozen decision-case business identity")
    runtime = initialize_runtime_storage(settings)
    ledger = DecisionLedger(runtime.engine)
    with ledger.serialize_case_execution() as connection:
        event = ledger.get_original_decision_event(case.business_object_id, connection)
        report = ledger.get_original_formal_report(case.business_object_id, connection)
        if event is None or report is None:
            raise ValueError("formal report must be published before notification")
        attempt = ledger.record_notification_attempt(
            connection,
            report=report,
            status=status,
            reasons=("SYNTHETIC_NOTIFICATION_FAILED",) if status == "FAILED" else (),
        )
        ledger.record_stage_result(
            connection,
            case=event.case,
            decision_event_id=report.event_id,
            stage_event_id=attempt.notification_attempt_id,
            stage_result=StageResult(
                phase="NOTIFICATION",
                status=attempt.status,
                gate_results=(GateResult(gate_id="FORMAL_REPORT_PUBLISHED", status="PASSED"),),
                reasons=attempt.reasons,
            ),
            recorded_at=attempt.recorded_at,
        )
    return attempt


def correct_default_frozen_decision_case(
    settings: Settings, business_identity: str
) -> DecisionCaseCorrection:
    """Append the D0 correction fact without replacing the original report."""
    case = load_frozen_decision_case(settings)
    if business_identity != case.business_identity:
        raise ValueError("unknown frozen decision-case business identity")
    runtime = initialize_runtime_storage(settings)
    ledger = DecisionLedger(runtime.engine)
    correction_error: DecisionEventCommitError | None = None
    publication_error: DecisionEventCommitError | None = None
    report: FormalReport | None = None
    with ledger.serialize_case_execution() as connection:
        original_event = ledger.get_original_decision_event(
            case.business_object_id,
            connection,
        )
        original_report = ledger.get_original_formal_report(
            case.business_object_id,
            connection,
        )
        if original_event is None or original_report is None:
            raise ValueError("a published original report is required before correction")
        correction_event = ledger.get_correction_event(
            original_event.decision_event_id,
            connection,
        )
        if correction_event is None:
            correction_case = _current_report_projection_case(original_event.case, case)
            correction_event_id = correction_case.correction_event_id(
                original_event.decision_event_id
            )
            correction_result = ExternalResult(
                outcome_code="SYNTHETIC_CORRECTION_RECORDED",
                summary=(
                    "Synthetic D0 correction recorded without replacing "
                    "the original decision event."
                ),
                key_reasons=("The original evidence cutoff and formal report remain available.",),
            )
            correction_stages = (
                StageResult(
                    phase="CORRECTION",
                    status="SUCCEEDED",
                    gate_results=(GateResult(gate_id="ORIGINAL_EVENT_RETAINED", status="PASSED"),),
                    reasons=("SYNTHETIC_CORRECTION_RECORDED",),
                ),
                StageResult(
                    phase="BUSINESS_COMMIT",
                    status="SUCCEEDED",
                    gate_results=(GateResult(gate_id="CORRECTION_RESULT_SAVED", status="PASSED"),),
                    reasons=(),
                ),
            )
            attempted_fact = ledger.build_event_fact(
                case=correction_case,
                framework_run_id=original_event.framework_run_id,
                result=correction_result,
                stage_results=correction_stages,
                decision_event_id=correction_event_id,
                corrects_event_id=original_event.decision_event_id,
                committed_at=FROZEN_CORRECTION_GENERATED_AT,
                generated_at=FROZEN_CORRECTION_GENERATED_AT,
            )
            try:
                correction_event = ledger.commit_event(
                    connection,
                    case=correction_case,
                    framework_run_id=original_event.framework_run_id,
                    result=correction_result,
                    stage_results=correction_stages,
                    decision_event_id=correction_event_id,
                    corrects_event_id=original_event.decision_event_id,
                    committed_at=FROZEN_CORRECTION_GENERATED_AT,
                    generated_at=FROZEN_CORRECTION_GENERATED_AT,
                )
            except DecisionEventCommitUncertainError as error:
                correction_event = ledger.reconcile_event_commit(connection, attempted_fact)
                if correction_event is None:
                    _record_correction_commit_failure(
                        ledger,
                        connection,
                        correction_case,
                        correction_event_id,
                        original_event.framework_run_id,
                        correction_stages[0],
                        uncertain=True,
                    )
                    correction_error = error
            except DecisionEventCommitError as error:
                correction_event = ledger.reconcile_event_commit(connection, attempted_fact)
                if correction_event is None:
                    _record_correction_commit_failure(
                        ledger,
                        connection,
                        correction_case,
                        correction_event_id,
                        original_event.framework_run_id,
                        correction_stages[0],
                        uncertain=False,
                    )
                    correction_error = error
        if correction_error is None:
            assert correction_event is not None
            correction_report_version_id = correction_event.case.correction_report_version_id(
                original_event.decision_event_id
            )
            report, publication_error = _publish_report_or_record_failure(
                ledger,
                connection,
                correction_event,
                correction_report_version_id,
            )
    if correction_error is not None:
        raise DecisionEventCommitError("correction commit failed") from correction_error
    if publication_error is not None:
        raise DecisionEventCommitError("correction publication failed") from publication_error
    assert report is not None
    return DecisionCaseCorrection(
        original_event_id=original_event.decision_event_id,
        original_report_version_id=original_report.report_version_id,
        report=report,
    )


def _run_frozen_decision_case(settings: Settings) -> DecisionCaseExecution:
    """Run or replay one exact frozen business identity without HTTP transport."""
    case = load_frozen_decision_case(settings)
    runtime = initialize_runtime_storage(settings)
    ledger = DecisionLedger(runtime.engine)
    ledger.persist_business_mapping_before_framework(case)
    with ledger.serialize_case_execution() as connection:
        existing_report = ledger.get_original_formal_report(
            case.business_object_id,
            connection,
        )
        if existing_report is not None:
            return _published_execution(existing_report)
        fact = ledger.get_original_decision_event(case.business_object_id, connection)

    if fact is None:
        with _serialize_local_framework_execution(case.business_object_id):
            with ledger.serialize_case_execution() as connection:
                existing_report = ledger.get_original_formal_report(
                    case.business_object_id,
                    connection,
                )
                if existing_report is not None:
                    return _published_execution(existing_report)
                fact = ledger.get_original_decision_event(case.business_object_id, connection)
                if fact is None:
                    mapping = ledger.get_business_object_mapping(
                        case.business_object_id,
                        connection,
                    )
                    if mapping is None:
                        raise RuntimeError(
                            "durable business mapping is missing before framework execution"
                        )
                    if mapping.case is None:
                        if (
                            mapping.case_id != case.case_id
                            or not case.matches_legacy_recovery_input()
                        ):
                            raise DecisionEventCommitError(
                                "business identity maps to different frozen input"
                            )
                        execution_case = case.legacy_projection_recovery_case(
                            mapping.framework_run_id
                        )
                    else:
                        execution_case = mapping.case
                else:
                    execution_case = None
            if fact is None:
                assert execution_case is not None
                try:
                    framework = asyncio.run(
                        execute_frozen_decision_case(
                            execution_case,
                            runtime,
                            lambda transition: _record_framework_transition(
                                ledger,
                                execution_case,
                                transition,
                            ),
                        )
                    )
                except ValueError as error:
                    raise DecisionEventCommitError("durable framework recovery failed") from error
                with ledger.serialize_case_execution() as connection:
                    existing_report = ledger.get_original_formal_report(
                        case.business_object_id,
                        connection,
                    )
                    if existing_report is not None:
                        return _published_execution(existing_report)
                    fact = ledger.get_original_decision_event(
                        case.business_object_id,
                        connection,
                    )
                    if fact is None:
                        committed_or_closed = _commit_framework_result(
                            ledger,
                            connection,
                            execution_case,
                            framework,
                        )
                        if isinstance(committed_or_closed, DecisionCaseExecution):
                            return committed_or_closed
                        fact = committed_or_closed

    with ledger.serialize_case_execution() as connection:
        existing_report = ledger.get_original_formal_report(
            case.business_object_id,
            connection,
        )
        if existing_report is not None:
            return _published_execution(existing_report)
        current_fact = ledger.get_original_decision_event(case.business_object_id, connection)
        if current_fact is None:
            raise RuntimeError("committed decision event is missing before publication")
        return _publish_committed_fact(ledger, connection, current_fact)


async def _record_framework_transition(
    ledger: DecisionLedger,
    case: FrozenDecisionCase,
    transition: FrameworkRunTransition,
) -> None:
    """Commit each already-durable M-Agent transition before more framework work begins."""
    with ledger.serialize_case_execution() as connection:
        ledger.record_stage_result(
            connection,
            case=case,
            stage_result=_framework_transition_stage_result(transition),
            framework_run_id=case.framework_run_id,
            allow_repeated_occurrence=transition.status in {"RUNNING", "WAITING"},
        )


@contextmanager
def _serialize_local_framework_execution(business_object_id: str) -> Iterator[None]:
    """Prevent same-process replays from racing one durable M-Agent Run lease."""
    with _FRAMEWORK_EXECUTION_LOCKS_GUARD:
        lock = _FRAMEWORK_EXECUTION_LOCKS.setdefault(business_object_id, Lock())
    with lock:
        yield


def _commit_framework_result(
    ledger: DecisionLedger,
    connection: Connection,
    execution_case: FrozenDecisionCase,
    framework: FrameworkRunResult,
) -> DecisionEventFact | DecisionCaseExecution:
    """Validate one terminal framework result and append its host business fact."""
    framework_stage_results = _framework_stage_results(execution_case, framework)
    durable_transition_count = (
        len(framework.transitions) if framework.transitions_durably_recorded else 0
    )
    for framework_stage_result in framework_stage_results[durable_transition_count:]:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=framework_stage_result,
            framework_run_id=execution_case.framework_run_id,
            allow_repeated_occurrence=framework_stage_result.status in {"RUNNING", "WAITING"},
        )
    framework_result = framework_stage_results[-1]
    if framework.run_id != execution_case.framework_run_id:
        return _unpublished_execution(
            execution_case,
            framework_run_status=framework_run_status_from_stage(framework_result),
            business_result_status=None,
            business_lifecycle=None,
            business_commit_status="NOT_ATTEMPTED",
            stage_results=ledger.get_stage_results(execution_case.business_object_id, connection),
        )
    if framework.status != "SUCCEEDED":
        return _unpublished_execution(
            execution_case,
            framework_run_status=framework.status,
            business_result_status=None,
            business_lifecycle=None,
            business_commit_status="NOT_ATTEMPTED",
            stage_results=ledger.get_stage_results(execution_case.business_object_id, connection),
        )
    if framework.output is None:
        validation_result = _failed_host_validation("OUTPUT_CONTRACT_MISSING")
        business_result: StageResult | None = None
    else:
        try:
            result = ExternalResult.model_validate_json(framework.output)
        except ValidationError:
            validation_result = _failed_host_validation("OUTPUT_CONTRACT_INVALID")
            business_result = None
        else:
            validation_result = host_validation_result(execution_case, result)
            business_result = (
                business_outcome_result(result) if validation_result.status == "SUCCEEDED" else None
            )
    ledger.record_stage_result(
        connection,
        case=execution_case,
        stage_result=validation_result,
        framework_run_id=execution_case.framework_run_id,
        allow_repeated_occurrence=validation_result.status
        not in {"SUCCEEDED", "REJECTED", "ABSTAINED"},
    )
    if business_result is not None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=business_result,
            framework_run_id=execution_case.framework_run_id,
        )
    framework_stage_results_before_commit = framework_stage_results[durable_transition_count:]
    current_stage_results_before_commit = (
        *framework_stage_results_before_commit,
        validation_result,
        *((business_result,) if business_result is not None else ()),
    )
    stage_results_before_commit = ledger.get_stage_results(
        execution_case.business_object_id,
        connection,
    )
    if business_result is None or not is_committable_host_outcome(business_result):
        return _unpublished_execution(
            execution_case,
            framework_run_status=framework.status,
            business_result_status=(
                business_result_status_from_stage(business_result)
                if business_result is not None
                else None
            ),
            business_lifecycle=(
                business_lifecycle_from_stage(business_result)
                if business_result is not None
                else None
            ),
            business_commit_status="NOT_ATTEMPTED",
            stage_results=stage_results_before_commit,
        )
    assert framework.output is not None
    result = ExternalResult.model_validate_json(framework.output)
    stage_results = (
        *stage_results_before_commit,
        StageResult(
            phase="BUSINESS_COMMIT",
            status="SUCCEEDED",
            gate_results=(GateResult(gate_id="HOST_RESULT_SAVED", status="PASSED"),),
            reasons=(),
        ),
    )
    attempted_fact = ledger.build_event_fact(
        case=execution_case,
        framework_run_id=framework.run_id,
        result=result,
        stage_results=stage_results,
    )
    try:
        return ledger.commit_event(
            connection,
            case=execution_case,
            framework_run_id=framework.run_id,
            result=result,
            stage_results=stage_results,
        )
    except DecisionEventCommitUncertainError:
        committed = ledger.reconcile_event_commit(connection, attempted_fact)
        if committed is not None:
            return committed
        _restore_precommit_stage_results(
            ledger,
            connection,
            execution_case,
            current_stage_results_before_commit,
        )
        uncertain_commit = StageResult(
            phase="COMMIT_RECONCILIATION",
            status="UNKNOWN",
            gate_results=(GateResult(gate_id="HOST_RESULT_SAVED", status="UNKNOWN"),),
            reasons=("COMMIT_UNCERTAIN",),
        )
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=uncertain_commit,
            framework_run_id=execution_case.framework_run_id,
            allow_repeated_occurrence=True,
        )
        return _unpublished_execution(
            execution_case,
            framework_run_status=framework.status,
            business_result_status=business_result_status_from_stage(business_result),
            business_lifecycle=business_lifecycle_from_stage(uncertain_commit),
            business_commit_status="UNKNOWN",
            stage_results=ledger.get_stage_results(execution_case.business_object_id, connection),
        )
    except DecisionEventCommitError:
        committed = ledger.reconcile_event_commit(connection, attempted_fact)
        if committed is not None:
            return committed
        _restore_precommit_stage_results(
            ledger,
            connection,
            execution_case,
            current_stage_results_before_commit,
        )
        failed_commit = StageResult(
            phase="BUSINESS_COMMIT",
            status="FAILED",
            gate_results=(GateResult(gate_id="HOST_RESULT_SAVED", status="FAILED"),),
            reasons=("COMMIT_STORAGE_FAILED",),
        )
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=failed_commit,
            framework_run_id=execution_case.framework_run_id,
            allow_repeated_occurrence=True,
        )
        return _unpublished_execution(
            execution_case,
            framework_run_status=framework.status,
            business_result_status=business_result_status_from_stage(business_result),
            business_lifecycle=None,
            business_commit_status="FAILED",
            stage_results=ledger.get_stage_results(execution_case.business_object_id, connection),
        )


def _publish_committed_fact(
    ledger: DecisionLedger,
    connection: Connection,
    fact: DecisionEventFact,
) -> DecisionCaseExecution:
    """Publish a committed host fact or append a closed publication result."""
    report, publication_error = _publish_report_or_record_failure(
        ledger,
        connection,
        fact,
    )
    if publication_error is not None:
        return _unpublished_execution(
            fact.case,
            framework_run_id=fact.framework_run_id,
            decision_event_id=fact.decision_event_id,
            report_version_id=fact.case.report_version_id_for_event(fact.decision_event_id),
            framework_run_status="SUCCEEDED",
            business_result_status=_business_result_status_from_stages(fact.stage_results),
            business_lifecycle=None,
            business_commit_status="COMMITTED",
            stage_results=ledger.get_stage_results(fact.business_object_id, connection),
        )
    assert report is not None
    return _published_execution(report)


def _publish_report_or_record_failure(
    ledger: DecisionLedger,
    connection: Connection,
    fact: DecisionEventFact,
    report_version_id: str | None = None,
) -> tuple[FormalReport | None, DecisionEventCommitError | None]:
    """Publish one committed fact or append the exact closed publication boundary."""
    resolved_report_version_id = report_version_id or fact.case.report_version_id_for_event(
        fact.decision_event_id
    )
    ledger.ensure_event_stage_results(connection, fact)
    report: FormalReport | None = None
    try:
        report = (
            ledger.publish_report(connection, fact)
            if report_version_id is None
            else ledger.publish_report(connection, fact, resolved_report_version_id)
        )
    except FormalReportCommitUncertainError as error:
        report = ledger.reconcile_report_commit(
            connection,
            fact.formal_report(resolved_report_version_id),
        )
        if report is None:
            _record_publication_failure(
                ledger,
                connection,
                fact,
                "PUBLICATION_COMMIT_UNCERTAIN",
            )
            return None, error
    except DecisionEventCommitError as error:
        _record_publication_failure(
            ledger,
            connection,
            fact,
            "PUBLICATION_STORAGE_FAILED",
        )
        return None, error
    assert report is not None
    _record_fact_stage_result(ledger, connection, fact, report.stage_results[-1])
    return report, None


def _record_publication_failure(
    ledger: DecisionLedger,
    connection: Connection,
    fact: DecisionEventFact,
    reason: str,
) -> None:
    """Discard unconfirmed output and retain the corresponding closed gate."""
    ledger.discard_unconfirmed_publication(connection)
    ledger.ensure_event_stage_results(connection, fact)
    _record_fact_stage_result(
        ledger,
        connection,
        fact,
        StageResult(
            phase="PUBLICATION",
            status="FAILED",
            gate_results=(GateResult(gate_id="EVENT_COMMITTED", status="PASSED"),),
            reasons=(reason,),
        ),
        allow_repeated_occurrence=True,
    )


def _record_fact_stage_result(
    ledger: DecisionLedger,
    connection: Connection,
    fact: DecisionEventFact,
    stage_result: StageResult,
    *,
    allow_repeated_occurrence: bool = False,
) -> None:
    """Append event-owned evidence using its distinct frozen generated clock."""
    if fact.generated_at is not None and fact.generated_at != fact.case.report_generated_at:
        ledger.record_stage_result(
            connection,
            case=fact.case,
            stage_result=stage_result,
            decision_event_id=fact.decision_event_id,
            framework_run_id=fact.framework_run_id,
            allow_repeated_occurrence=allow_repeated_occurrence,
            recorded_at=fact.generated_at,
        )
        return
    ledger.record_stage_result(
        connection,
        case=fact.case,
        stage_result=stage_result,
        decision_event_id=fact.decision_event_id,
        framework_run_id=fact.framework_run_id,
        allow_repeated_occurrence=allow_repeated_occurrence,
    )


def get_formal_report(report_version_id: str, settings: Settings) -> FormalReport | None:
    """Read only an already committed formal report."""
    return DecisionLedger.from_settings(settings).get_formal_report(report_version_id)


def _published_execution(report: FormalReport) -> DecisionCaseExecution:
    return DecisionCaseExecution(
        business_object_id=report.business_object_id,
        framework_run_id=report.framework_run_id,
        decision_event_id=report.event_id,
        report_version_id=report.report_version_id,
        framework_run_status=_framework_run_status_from_stage_history(report.stage_results),
        business_result_status=_business_result_status_from_stages(report.stage_results),
        business_lifecycle=_business_lifecycle_from_stages(report.stage_results),
        business_commit_status="COMMITTED",
        publication_status="PUBLISHED",
        report=report,
        stage_results=report.stage_results,
    )


def _unpublished_execution(
    case: FrozenDecisionCase,
    *,
    framework_run_id: str | None = None,
    decision_event_id: str | None = None,
    report_version_id: str | None = None,
    framework_run_status: FrameworkRunStatus,
    business_result_status: BusinessResultStatus | None,
    business_lifecycle: BusinessLifecycle | None,
    business_commit_status: BusinessCommitStatus,
    stage_results: tuple[StageResult, ...],
) -> DecisionCaseExecution:
    """Expose a closed publication gate without inventing a formal report."""
    return DecisionCaseExecution(
        business_object_id=case.business_object_id,
        framework_run_id=framework_run_id or case.framework_run_id,
        decision_event_id=decision_event_id or case.decision_event_id,
        report_version_id=report_version_id or case.report_version_id,
        framework_run_status=framework_run_status,
        business_result_status=business_result_status,
        business_lifecycle=business_lifecycle,
        business_commit_status=business_commit_status,
        publication_status="CLOSED",
        report=None,
        stage_results=stage_results,
    )


def _framework_stage_result(case: FrozenDecisionCase, framework: FrameworkRunResult) -> StageResult:
    """Save the framework state before evaluating any host-owned result."""
    if framework.run_id != case.framework_run_id:
        return StageResult(
            phase="FRAMEWORK_RUN",
            status="FAILED",
            gate_results=(GateResult(gate_id="ORIGINAL_RUN_IDENTITY", status="FAILED"),),
            reasons=("FRAMEWORK_IDENTITY_MISMATCH",),
        )
    if framework.status in {"SUCCEEDED", "REJECTED", "FAILED", "CANCELLED"}:
        return StageResult(
            phase="FRAMEWORK_RUN",
            status=framework.status,
            gate_results=(
                GateResult(gate_id="RUN_TERMINAL", status="PASSED"),
                GateResult(
                    gate_id="FRAMEWORK_EXECUTION",
                    status="PASSED" if framework.status == "SUCCEEDED" else "FAILED",
                ),
            ),
            reasons=(
                ("FRAMEWORK_RUN_SUCCEEDED",)
                if framework.status == "SUCCEEDED"
                else (
                    framework.error_code
                    or framework.waiting_reason
                    or f"FRAMEWORK_{framework.status}",
                )
            ),
        )
    return StageResult(
        phase="FRAMEWORK_RUN",
        status=framework.status,
        gate_results=(GateResult(gate_id="RUN_TERMINAL", status="FAILED"),),
        reasons=(
            framework.error_code or framework.waiting_reason or f"FRAMEWORK_{framework.status}",
        ),
    )


def _framework_stage_results(
    case: FrozenDecisionCase, framework: FrameworkRunResult
) -> tuple[StageResult, ...]:
    """Keep framework creation and recovery observations distinct from its outcome."""
    final_result = _framework_stage_result(case, framework)
    if framework.run_id != case.framework_run_id:
        return (final_result,)
    transitions = tuple(
        _framework_transition_stage_result(transition) for transition in framework.transitions
    )
    if transitions and transitions[-1] == final_result:
        return transitions
    return (*transitions, final_result)


def _framework_transition_stage_result(
    transition: FrameworkRunTransition,
) -> StageResult:
    gate_id = {
        "CREATED": "RUN_CREATED",
        "RUNNING": "RUN_ACTIVE",
        "WAITING": "RUN_RECOVERABLE",
    }.get(transition.status, "RUN_RECOVERABLE")
    return StageResult(
        phase="FRAMEWORK_RUN",
        status=transition.status,
        gate_results=(GateResult(gate_id=gate_id, status="PASSED"),),
        reasons=(transition.reason,),
    )


def _framework_run_status_from_stage_history(
    stage_results: tuple[StageResult, ...],
) -> FrameworkRunStatus:
    for stage_result in reversed(stage_results):
        if stage_result.phase == "FRAMEWORK_RUN":
            return framework_run_status_from_stage(stage_result)
    raise RuntimeError("report has no framework run state")


def _restore_precommit_stage_results(
    ledger: DecisionLedger,
    connection: Connection,
    case: FrozenDecisionCase,
    stage_results: tuple[StageResult, ...],
) -> None:
    """Restore durable pre-commit evidence after an event write transaction rolls back."""
    for stage_result in stage_results:
        ledger.record_stage_result(
            connection,
            case=case,
            stage_result=stage_result,
            framework_run_id=case.framework_run_id,
            allow_repeated_occurrence=stage_result.status
            not in {"SUCCEEDED", "REJECTED", "ABSTAINED"},
        )


def _failed_host_validation(reason: str) -> StageResult:
    return StageResult(
        phase="HOST_VALIDATION",
        status="FAILED",
        gate_results=(GateResult(gate_id="OUTPUT_CONTRACT", status="FAILED"),),
        reasons=(reason,),
    )


def _business_result_status_from_stages(
    stage_results: tuple[StageResult, ...],
) -> BusinessResultStatus | None:
    for stage_result in reversed(stage_results):
        if stage_result.phase == "BUSINESS_DECISION":
            return business_result_status_from_stage(stage_result)
    return None


def _business_lifecycle_from_stages(
    stage_results: tuple[StageResult, ...],
) -> BusinessLifecycle | None:
    for stage_result in reversed(stage_results):
        lifecycle = business_lifecycle_from_stage(stage_result)
        if lifecycle is not None:
            return lifecycle
    return None


def _record_correction_commit_failure(
    ledger: DecisionLedger,
    connection: Connection,
    case: FrozenDecisionCase,
    correction_event_id: str,
    framework_run_id: str,
    correction_stage: StageResult,
    *,
    uncertain: bool,
) -> None:
    """Keep correction intent and its failed save boundary as append-only evidence."""
    ledger.record_stage_result(
        connection,
        case=case,
        decision_event_id=correction_event_id,
        framework_run_id=framework_run_id,
        stage_result=correction_stage,
        recorded_at=FROZEN_CORRECTION_GENERATED_AT,
    )
    ledger.record_stage_result(
        connection,
        case=case,
        decision_event_id=correction_event_id,
        framework_run_id=framework_run_id,
        stage_result=StageResult(
            phase="COMMIT_RECONCILIATION" if uncertain else "BUSINESS_COMMIT",
            status="UNKNOWN" if uncertain else "FAILED",
            gate_results=(
                GateResult(
                    gate_id="CORRECTION_RESULT_SAVED",
                    status="UNKNOWN" if uncertain else "FAILED",
                ),
            ),
            reasons=("COMMIT_UNCERTAIN",) if uncertain else ("COMMIT_STORAGE_FAILED",),
        ),
        allow_repeated_occurrence=True,
        recorded_at=FROZEN_CORRECTION_GENERATED_AT,
    )


def _current_report_projection_case(
    original_case: FrozenDecisionCase,
    current_case: FrozenDecisionCase,
) -> FrozenDecisionCase:
    """Write corrections under the build that creates them without replacing old evidence."""
    return original_case.model_copy(
        update={
            "version_bundle": current_case.version_bundle.model_copy(
                update={
                    "report_projection_contract_version": (
                        FROZEN_REPORT_PROJECTION_CONTRACT_VERSION
                    )
                }
            ),
            "recovery_framework_run_id": original_case.framework_run_id,
        }
    )
