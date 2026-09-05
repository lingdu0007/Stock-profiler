"""Host-owned frozen decision journey: Run evidence, validate, commit, then publish."""

from __future__ import annotations

import asyncio

from pydantic import ValidationError

from stock_profiler.adapters.m_agent.frozen_decision_case import (
    FrameworkRunResult,
    execute_frozen_decision_case,
)
from stock_profiler.adapters.persistence.decision_ledger import (
    DecisionEventCommitError,
    DecisionEventCommitUncertainError,
    DecisionLedger,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    FROZEN_CORRECTION_GENERATED_AT,
    BusinessCommitStatus,
    BusinessLifecycleStatus,
    BusinessResultStatus,
    DecisionCaseCorrection,
    DecisionCaseExecution,
    ExternalResult,
    FormalReport,
    FrameworkRunStatus,
    FrozenDecisionCase,
    GateResult,
    NotificationAttempt,
    NotificationAttemptStatus,
    StageResult,
    business_lifecycle_status_from_stage,
    business_result_status_from_stage,
    framework_run_status_from_stage,
    host_validation_result,
    is_committable_host_validation_result,
    load_frozen_decision_case,
)


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
            original_case = original_event.case
            correction_event_id = original_case.correction_event_id(
                original_event.decision_event_id
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
            correction_event = ledger.commit_event(
                connection,
                case=original_case,
                framework_run_id=original_event.framework_run_id,
                result=ExternalResult(
                    outcome_code="SYNTHETIC_CORRECTION_RECORDED",
                    summary=(
                        "Synthetic D0 correction recorded without replacing "
                        "the original decision event."
                    ),
                    key_reasons=(
                        "The original evidence cutoff and formal report remain available.",
                    ),
                ),
                stage_results=correction_stages,
                decision_event_id=correction_event_id,
                corrects_event_id=original_event.decision_event_id,
                committed_at=FROZEN_CORRECTION_GENERATED_AT,
                generated_at=FROZEN_CORRECTION_GENERATED_AT,
            )
        ledger.ensure_event_stage_results(connection, correction_event)
        report = ledger.get_formal_report_for_event(
            correction_event.decision_event_id,
            connection,
        )
        if report is None:
            correction_report_version_id = correction_event.case.correction_report_version_id(
                original_event.decision_event_id
            )
            report = ledger.publish_report(
                connection,
                correction_event,
                correction_report_version_id,
            )
        ledger.record_stage_result(
            connection,
            case=correction_event.case,
            decision_event_id=correction_event.decision_event_id,
            stage_result=report.stage_results[-1],
        )
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
            mapping = ledger.get_business_object_mapping(case.business_object_id, connection)
            if mapping is None:
                raise RuntimeError("durable business mapping is missing before framework execution")
            if mapping.case is None:
                if mapping.frozen_input_fingerprint != case.frozen_input_fingerprint:
                    raise DecisionEventCommitError(
                        "business mapping has no recoverable frozen case snapshot"
                    )
                execution_case = case
            elif not mapping.case.matches_recovery_input(case):
                raise DecisionEventCommitError("business identity maps to different frozen input")
            else:
                execution_case = mapping.case
            framework = asyncio.run(execute_frozen_decision_case(execution_case, runtime))
            framework_result = _framework_stage_result(execution_case, framework)
            ledger.record_stage_result(
                connection,
                case=execution_case,
                stage_result=framework_result,
                append=framework_result.status != "SUCCEEDED",
            )
            if framework.run_id != execution_case.framework_run_id:
                return _unpublished_execution(
                    execution_case,
                    framework_run_status=framework_run_status_from_stage(framework_result),
                    business_result_status=None,
                    business_lifecycle_status=None,
                    business_commit_status="NOT_ATTEMPTED",
                    stage_results=(framework_result,),
                )
            if framework.status != "SUCCEEDED":
                return _unpublished_execution(
                    execution_case,
                    framework_run_status=framework.status,
                    business_result_status=None,
                    business_lifecycle_status=None,
                    business_commit_status="NOT_ATTEMPTED",
                    stage_results=(framework_result,),
                )
            if framework.output is None:
                validation_result = _failed_host_validation("OUTPUT_CONTRACT_MISSING")
            else:
                try:
                    result = ExternalResult.model_validate_json(framework.output)
                except ValidationError:
                    validation_result = _failed_host_validation("OUTPUT_CONTRACT_INVALID")
                else:
                    validation_result = host_validation_result(execution_case, result)
            ledger.record_stage_result(
                connection,
                case=execution_case,
                stage_result=validation_result,
                append=validation_result.status not in {"SUCCEEDED", "REJECTED", "ABSTAINED"},
            )
            stage_results_before_commit = (framework_result, validation_result)
            if not is_committable_host_validation_result(validation_result):
                return _unpublished_execution(
                    execution_case,
                    framework_run_status=framework.status,
                    business_result_status=business_result_status_from_stage(validation_result),
                    business_lifecycle_status=business_lifecycle_status_from_stage(
                        validation_result
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
            try:
                fact = ledger.commit_event(
                    connection,
                    case=execution_case,
                    framework_run_id=framework.run_id,
                    result=result,
                    stage_results=stage_results,
                )
            except DecisionEventCommitUncertainError:
                committed = ledger.get_decision_event(
                    execution_case.decision_event_id,
                    connection,
                )
                if committed is not None:
                    fact = committed
                else:
                    uncertain_commit = StageResult(
                        phase="BUSINESS_COMMIT",
                        status="UNKNOWN",
                        gate_results=(
                            GateResult(
                                gate_id="HOST_RESULT_SAVED",
                                status="UNKNOWN",
                            ),
                        ),
                        reasons=("COMMIT_UNCERTAIN",),
                    )
                    ledger.record_stage_result(
                        connection,
                        case=execution_case,
                        stage_result=uncertain_commit,
                        append=True,
                    )
                    return _unpublished_execution(
                        execution_case,
                        framework_run_status=framework.status,
                        business_result_status=business_result_status_from_stage(validation_result),
                        business_lifecycle_status=None,
                        business_commit_status="UNKNOWN",
                        stage_results=(
                            *stage_results[:-1],
                            uncertain_commit,
                        ),
                    )
            except DecisionEventCommitError:
                committed = ledger.get_decision_event(
                    execution_case.decision_event_id,
                    connection,
                )
                if committed is not None:
                    fact = committed
                else:
                    failed_commit = StageResult(
                        phase="BUSINESS_COMMIT",
                        status="FAILED",
                        gate_results=(
                            GateResult(
                                gate_id="HOST_RESULT_SAVED",
                                status="FAILED",
                            ),
                        ),
                        reasons=("COMMIT_STORAGE_FAILED",),
                    )
                    ledger.record_stage_result(
                        connection,
                        case=execution_case,
                        stage_result=failed_commit,
                        append=True,
                    )
                    return _unpublished_execution(
                        execution_case,
                        framework_run_status=framework.status,
                        business_result_status=business_result_status_from_stage(validation_result),
                        business_lifecycle_status=None,
                        business_commit_status="FAILED",
                        stage_results=(
                            *stage_results[:-1],
                            failed_commit,
                        ),
                    )
        assert fact is not None
        ledger.ensure_event_stage_results(connection, fact)
        try:
            report = ledger.publish_report(connection, fact)
        except DecisionEventCommitError:
            ledger.discard_unconfirmed_publication(connection)
            ledger.ensure_event_stage_results(connection, fact)
            publication_failure = StageResult(
                phase="PUBLICATION",
                status="FAILED",
                gate_results=(GateResult(gate_id="EVENT_COMMITTED", status="PASSED"),),
                reasons=("PUBLICATION_STORAGE_FAILED",),
            )
            ledger.record_stage_result(
                connection,
                case=fact.case,
                stage_result=publication_failure,
                decision_event_id=fact.decision_event_id,
                append=True,
            )
            return _unpublished_execution(
                fact.case,
                framework_run_id=fact.framework_run_id,
                decision_event_id=fact.decision_event_id,
                report_version_id=fact.case.report_version_id_for_event(fact.decision_event_id),
                framework_run_status="SUCCEEDED",
                business_result_status=_business_result_status_from_stages(fact.stage_results),
                business_lifecycle_status=None,
                business_commit_status="COMMITTED",
                stage_results=(*fact.stage_results, publication_failure),
            )
        ledger.record_stage_result(
            connection,
            case=fact.case,
            stage_result=report.stage_results[-1],
            decision_event_id=fact.decision_event_id,
        )
    return _published_execution(report)


def get_formal_report(report_version_id: str, settings: Settings) -> FormalReport | None:
    """Read only an already committed formal report."""
    return DecisionLedger.from_settings(settings).get_formal_report(report_version_id)


def _published_execution(report: FormalReport) -> DecisionCaseExecution:
    return DecisionCaseExecution(
        business_object_id=report.business_object_id,
        framework_run_id=report.framework_run_id,
        decision_event_id=report.event_id,
        report_version_id=report.report_version_id,
        framework_run_status=framework_run_status_from_stage(report.stage_results[0]),
        business_result_status=_business_result_status_from_stages(report.stage_results),
        business_lifecycle_status=None,
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
    business_lifecycle_status: BusinessLifecycleStatus | None,
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
        business_lifecycle_status=business_lifecycle_status,
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
    if framework.status == "SUCCEEDED":
        return StageResult(
            phase="FRAMEWORK_RUN",
            status="SUCCEEDED",
            gate_results=(GateResult(gate_id="RUN_TERMINAL", status="PASSED"),),
            reasons=(),
        )
    return StageResult(
        phase="FRAMEWORK_RUN",
        status=framework.status,
        gate_results=(GateResult(gate_id="RUN_TERMINAL", status="FAILED"),),
        reasons=(
            framework.error_code or framework.waiting_reason or f"FRAMEWORK_{framework.status}",
        ),
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
    for stage_result in stage_results:
        if stage_result.phase == "HOST_VALIDATION":
            return business_result_status_from_stage(stage_result)
    return None
