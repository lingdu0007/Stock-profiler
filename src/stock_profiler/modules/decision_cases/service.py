"""Host-owned frozen decision journey: Run evidence, validate, commit, then publish."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

from pydantic import ValidationError

from stock_profiler.adapters.m_agent.frozen_decision_case import (
    FrameworkRunResult,
    execute_frozen_decision_case,
)
from stock_profiler.adapters.persistence.decision_ledger import (
    DecisionEventCommitError,
    DecisionLedger,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    BusinessCommitStatus,
    DecisionCaseCorrection,
    DecisionCaseExecution,
    DecisionEventFact,
    DecisionResultStatus,
    ExternalResult,
    FormalReport,
    FrameworkRunStatus,
    FrozenDecisionCase,
    GateResult,
    NotificationAttempt,
    StageResult,
    host_validation_result,
    load_frozen_decision_case,
)

_COMMITTABLE_HOST_RESULTS = frozenset(
    {"SUCCEEDED", "REJECTED", "ABSTAINED", "EXPIRED", "EXECUTION_BLOCKED"}
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
    deliver: Callable[[FormalReport], None],
) -> NotificationAttempt:
    """Record one recoverable D0 notification attempt for an existing report."""
    case = load_frozen_decision_case(settings)
    if business_identity != case.business_identity:
        raise ValueError("unknown frozen decision-case business identity")
    runtime = initialize_runtime_storage(settings)
    ledger = DecisionLedger(runtime.engine)
    with ledger.serialize_case_execution() as connection:
        report = ledger.get_formal_report(case.report_version_id, connection)
        if report is None:
            raise ValueError("formal report must be published before notification")
        try:
            deliver(report)
        except Exception:
            attempt = ledger.record_notification_attempt(
                connection,
                report=report,
                status="FAILED",
                reasons=("NOTIFICATION_DELIVERY_FAILED",),
            )
        else:
            attempt = ledger.record_notification_attempt(
                connection,
                report=report,
                status="SUCCEEDED",
                reasons=(),
            )
        ledger.record_stage_result(
            connection,
            case=case,
            decision_event_id=report.event_id,
            stage_event_id=attempt.notification_attempt_id,
            stage_result=StageResult(
                phase="NOTIFICATION",
                status=attempt.status,
                gate_results=(
                    GateResult(gate_id="FORMAL_REPORT_PUBLISHED", status="PASSED"),
                ),
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
        original_event = ledger.get_decision_event(case.decision_event_id, connection)
        original_report = ledger.get_formal_report(case.report_version_id, connection)
        if original_event is None or original_report is None:
            raise ValueError("a published original report is required before correction")
        correction_event_id = case.correction_event_id(original_event.decision_event_id)
        correction_report_version_id = case.correction_report_version_id(
            original_event.decision_event_id
        )
        correction_event = ledger.get_decision_event(correction_event_id, connection)
        if correction_event is None:
            correction_stages = (
                StageResult(
                    phase="CORRECTION",
                    status="SUCCEEDED",
                    gate_results=(
                        GateResult(gate_id="ORIGINAL_EVENT_RETAINED", status="PASSED"),
                    ),
                    reasons=("SYNTHETIC_CORRECTION_RECORDED",),
                ),
                StageResult(
                    phase="BUSINESS_COMMIT",
                    status="SUCCEEDED",
                    gate_results=(
                        GateResult(gate_id="CORRECTION_RESULT_SAVED", status="PASSED"),
                    ),
                    reasons=(),
                ),
            )
            correction_event = ledger.commit_event(
                connection,
                case=case,
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
            )
            for stage_result in correction_stages:
                ledger.record_stage_result(
                    connection,
                    case=case,
                    decision_event_id=correction_event.decision_event_id,
                    stage_result=stage_result,
                )
        report = ledger.publish_report(
            connection,
            correction_event,
            correction_report_version_id,
        )
        ledger.record_stage_result(
            connection,
            case=case,
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
    with ledger.serialize_case_execution() as connection:
        existing_report = ledger.get_formal_report(case.report_version_id, connection)
        if existing_report is not None:
            return _execution(case, existing_report)

        fact = ledger.get_decision_event(case.decision_event_id, connection)
        if fact is None:
            ledger.ensure_business_object(connection, case)
            framework = asyncio.run(execute_frozen_decision_case(case, runtime))
            framework_result = _framework_stage_result(case, framework)
            ledger.record_stage_result(
                connection,
                case=case,
                stage_result=framework_result,
            )
            if framework.run_id != case.framework_run_id:
                return _unpublished_execution(
                    case,
                    framework_run_status="FAILED",
                    business_result_status=None,
                    business_commit_status="NOT_ATTEMPTED",
                    stage_results=(
                        StageResult(
                            phase="FRAMEWORK_RUN",
                            status="FAILED",
                            gate_results=(
                                GateResult(
                                    gate_id="ORIGINAL_RUN_IDENTITY",
                                    status="FAILED",
                                ),
                            ),
                            reasons=("FRAMEWORK_IDENTITY_MISMATCH",),
                        ),
                    ),
                )
            if framework.status != "SUCCEEDED":
                return _unpublished_execution(
                    case,
                    framework_run_status=framework.status,
                    business_result_status=None,
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
                    validation_result = host_validation_result(case, result)
            ledger.record_stage_result(
                connection,
                case=case,
                stage_result=validation_result,
            )
            stage_results_before_commit = (framework_result, validation_result)
            if validation_result.status not in _COMMITTABLE_HOST_RESULTS:
                return _unpublished_execution(
                    case,
                    framework_run_status=framework.status,
                    business_result_status=_host_stage_status(validation_result),
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
                    case=case,
                    framework_run_id=framework.run_id,
                    result=result,
                    stage_results=stage_results,
                )
            except DecisionEventCommitError:
                committed = ledger.get_decision_event(case.decision_event_id, connection)
                if committed is not None:
                    fact = committed
                else:
                    uncertain_commit = StageResult(
                        phase="BUSINESS_COMMIT",
                        status="UNKNOWN",
                        gate_results=(
                            GateResult(
                                gate_id="HOST_RESULT_SAVED",
                                status="FAILED",
                            ),
                        ),
                        reasons=("COMMIT_UNCERTAIN",),
                    )
                    ledger.record_stage_result(
                        connection,
                        case=case,
                        stage_result=uncertain_commit,
                    )
                    return _unpublished_execution(
                        case,
                        framework_run_status=framework.status,
                        business_result_status=_host_stage_status(validation_result),
                        business_commit_status="UNKNOWN",
                        stage_results=(
                            *stage_results[:-1],
                            uncertain_commit,
                        ),
                    )
            ledger.record_stage_result(
                connection,
                case=case,
                stage_result=stage_results[2],
                decision_event_id=fact.decision_event_id,
            )
        try:
            report = ledger.publish_report(connection, fact)
        except DecisionEventCommitError:
            publication_failure = StageResult(
                phase="PUBLICATION",
                status="FAILED",
                gate_results=(GateResult(gate_id="EVENT_COMMITTED", status="PASSED"),),
                reasons=("PUBLICATION_STORAGE_FAILED",),
            )
            ledger.record_stage_result(
                connection,
                case=case,
                stage_result=publication_failure,
                decision_event_id=fact.decision_event_id,
            )
            return _unpublished_execution(
                case,
                framework_run_status="SUCCEEDED",
                business_result_status=_host_result_status(fact),
                business_commit_status="COMMITTED",
                stage_results=(*fact.stage_results, publication_failure),
            )
        ledger.record_stage_result(
            connection,
            case=case,
            stage_result=report.stage_results[-1],
            decision_event_id=fact.decision_event_id,
        )
    return _execution(case, report)


def get_formal_report(report_version_id: str, settings: Settings) -> FormalReport | None:
    """Read only an already committed formal report."""
    return DecisionLedger.from_settings(settings).get_formal_report(report_version_id)


def _execution(case: FrozenDecisionCase, report: FormalReport) -> DecisionCaseExecution:
    return DecisionCaseExecution(
        business_object_id=case.business_object_id,
        framework_run_id=case.framework_run_id,
        decision_event_id=case.decision_event_id,
        report_version_id=case.report_version_id,
        framework_run_status=_framework_run_status(report),
        business_result_status=_host_result_status_from_stages(report.stage_results),
        business_commit_status="COMMITTED",
        publication_status="PUBLISHED",
        report=report,
        stage_results=report.stage_results,
    )


def _unpublished_execution(
    case: FrozenDecisionCase,
    *,
    framework_run_status: FrameworkRunStatus,
    business_result_status: DecisionResultStatus | None,
    business_commit_status: BusinessCommitStatus,
    stage_results: tuple[StageResult, ...],
) -> DecisionCaseExecution:
    """Expose a closed publication gate without inventing a formal report."""
    return DecisionCaseExecution(
        business_object_id=case.business_object_id,
        framework_run_id=case.framework_run_id,
        decision_event_id=case.decision_event_id,
        report_version_id=case.report_version_id,
        framework_run_status=framework_run_status,
        business_result_status=business_result_status,
        business_commit_status=business_commit_status,
        publication_status="CLOSED",
        report=None,
        stage_results=stage_results,
    )


def _framework_stage_result(
    case: FrozenDecisionCase, framework: FrameworkRunResult
) -> StageResult:
    """Save the framework state before evaluating any host-owned result."""
    if framework.run_id != case.framework_run_id:
        return StageResult(
            phase="FRAMEWORK_RUN",
            status="FAILED",
            gate_results=(
                GateResult(gate_id="ORIGINAL_RUN_IDENTITY", status="FAILED"),
            ),
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
        reasons=(framework.waiting_reason or f"FRAMEWORK_{framework.status}",),
    )


def _failed_host_validation(reason: str) -> StageResult:
    return StageResult(
        phase="HOST_VALIDATION",
        status="FAILED",
        gate_results=(GateResult(gate_id="OUTPUT_CONTRACT", status="FAILED"),),
        reasons=(reason,),
    )


def _framework_run_status(report: FormalReport) -> FrameworkRunStatus:
    for stage_result in report.stage_results:
        if stage_result.phase == "FRAMEWORK_RUN":
            if stage_result.status in {
                "CREATED",
                "RUNNING",
                "WAITING",
                "SUCCEEDED",
                "REJECTED",
                "FAILED",
                "CANCELLED",
            }:
                return cast(FrameworkRunStatus, stage_result.status)
            raise RuntimeError("formal report has an invalid framework run state")
    return "FAILED"


def _host_result_status(fact: DecisionEventFact) -> DecisionResultStatus:
    return _host_result_status_from_stages(fact.stage_results)


def _host_result_status_from_stages(
    stage_results: tuple[StageResult, ...],
) -> DecisionResultStatus:
    for stage_result in stage_results:
        if stage_result.phase == "HOST_VALIDATION":
            return _host_stage_status(stage_result)
    return "FAILED"


def _host_stage_status(stage_result: StageResult) -> DecisionResultStatus:
    if stage_result.status in {
        "SUCCEEDED",
        "REJECTED",
        "ABSTAINED",
        "FAILED",
        "PENDING",
        "EXPIRED",
        "EXECUTION_BLOCKED",
        "UNKNOWN",
    }:
        return cast(DecisionResultStatus, stage_result.status)
    raise RuntimeError("host validation stage has a framework-only state")
