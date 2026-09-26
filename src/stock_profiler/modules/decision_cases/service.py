"""Host-owned frozen decision journey: Run evidence, validate, commit, then publish."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from threading import Lock
from typing import Literal

from pydantic import ValidationError

from stock_profiler.modules.candidate_selection.selection import freeze_selection
from stock_profiler.modules.candidate_selection.universe import freeze_universe
from stock_profiler.modules.decision_cases.domain import (
    FROZEN_REPORT_PROJECTION_CONTRACT_VERSION,
    BusinessCommitStatus,
    BusinessLifecycle,
    BusinessResultStatus,
    CorrectionEvidence,
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
    research_contract_mode_for_case,
)
from stock_profiler.modules.decision_cases.execution_plans import adjudicate_execution_plan
from stock_profiler.modules.decision_cases.frozen_case import load_frozen_correction_payload
from stock_profiler.modules.decision_cases.monitoring import assess_monitoring
from stock_profiler.modules.decision_cases.ports import (
    BusinessObjectMapping,
    DecisionEventCommitError,
    DecisionEventCommitUncertainError,
    DecisionLedger,
    FormalReportCommitUncertainError,
    FrameworkRunResult,
    FrameworkRunTransition,
    FrozenFramework,
    MappedDurableRunMissingError,
    ResearchMemberRunResult,
    Transaction,
)
from stock_profiler.modules.portfolio.contracts import (
    PortfolioAuthorizationOutcome,
    PortfolioUseCommand,
    portfolio_id_for,
)
from stock_profiler.modules.portfolio.drawdown import adjudicate as adjudicate_drawdown
from stock_profiler.modules.portfolio.liquidity import assess_liquidity
from stock_profiler.modules.portfolio.service import adjudicate as adjudicate_portfolio
from stock_profiler.modules.portfolio.stress import assess_stress
from stock_profiler.modules.position_management.concentration import assess_concentration
from stock_profiler.modules.position_management.service import reconcile as reconcile_position
from stock_profiler.modules.qualification.governance import adjudicate, validate_new_request
from stock_profiler.modules.research.contracts import (
    RAW_SCORE_FEATURE_DATA_TYPES,
    RawScoreCalculationError,
    ResearchCommand,
    ResearchDraft,
    ResearchFrameworkOutput,
    ResearchRiskPlan,
    RiskVetoDraft,
    decode_historical_research_draft,
    decode_historical_research_framework_output,
    decode_legacy_research_draft,
    decode_legacy_research_framework_output,
    research_draft_payload,
    research_evidence_payload,
    research_member_handoff_payload,
    research_outcome_raw_score_payloads,
    research_raw_score_payload,
)
from stock_profiler.modules.research.service import (
    freeze_research,
    prepare_research_risk_plan,
    validate_research_draft,
)

_FRAMEWORK_EXECUTION_LOCKS: dict[str, Lock] = {}
_FRAMEWORK_EXECUTION_LOCKS_GUARD = Lock()


def get_formal_report(
    report_version_id: str, ledger: DecisionLedger[Transaction]
) -> FormalReport | None:
    """Read only the verified saved projection through the host-owned port."""
    return ledger.get_formal_report(report_version_id)


def run_default_frozen_decision_case(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    framework: FrozenFramework,
) -> DecisionCaseExecution:
    """Run the published public fixture through the same host module used by every entrypoint."""
    return _run_frozen_decision_case(case, ledger, framework)


def _validate_research_selection_event(
    case: FrozenDecisionCase,
    selection_event: DecisionEventFact | None,
) -> None:
    """Require research to consume the committed upstream selection fact."""
    command = case.research
    assert command is not None
    if selection_event is None:
        raise ValueError("RESEARCH_SELECTION_EVENT_MISSING")
    if (
        selection_event.decision_event_id != command.selection_event_id
        or selection_event.corrects_event_id is not None
        or selection_event.validation_status != "PASSED"
    ):
        raise ValueError("RESEARCH_SELECTION_EVENT_INVALID")
    source_case = selection_event.case
    source_selection = source_case.selection
    source_outcome = selection_event.result.selection
    if source_selection is None or source_outcome is None:
        raise ValueError("RESEARCH_SELECTION_HANDOFF_MISSING")
    if (
        selection_event.business_object_id != command.selection_object_id
        or source_case.business_object_id != command.selection_object_id
    ):
        raise ValueError("RESEARCH_SELECTION_OBJECT_MISMATCH")
    if (
        case.access_scope is None
        or source_case.access_scope is None
        or not case.access_scope.same_scope_as(source_case.access_scope)
    ):
        raise ValueError("RESEARCH_SELECTION_SCOPE_MISMATCH")
    if (
        source_selection.cutoff_at != command.cutoff_at
        or source_outcome.cutoff_at != command.cutoff_at
        or datetime.fromisoformat(source_case.knowledge_cutoff) != command.knowledge_cutoff
    ):
        raise ValueError("RESEARCH_SELECTION_CUTOFF_MISMATCH")
    if source_outcome.disposition != "FROZEN":
        raise ValueError("RESEARCH_SELECTION_NOT_FROZEN")
    if tuple(source_outcome.members) != tuple(command.screening.selected_member_ids):
        raise ValueError("RESEARCH_SELECTION_COHORT_MISMATCH")
    source_rows = tuple(source_selection.rows)
    if tuple(row.security_id for row in source_rows) != tuple(
        command.screening.universe_security_ids
    ):
        raise ValueError("RESEARCH_SELECTION_UNIVERSE_MISMATCH")
    if (
        source_selection.screening_snapshot_id != command.screening.snapshot_id
        or source_selection.strategy_version != command.screening.strategy_version
    ):
        raise ValueError("RESEARCH_SELECTION_SCREENING_VERSION_MISMATCH")
    source_positive_scores = {row.security_id: row.positive_score for row in source_rows}
    source_terminal_scores = {row.security_id: row.terminal_score for row in source_rows}
    if (
        any(score is None for score in source_positive_scores.values())
        or any(score is None for score in source_terminal_scores.values())
        or source_positive_scores != command.screening.positive_scores
        or source_terminal_scores != command.screening.terminal_scores
    ):
        raise ValueError("RESEARCH_SELECTION_SCREENING_MISMATCH")
    source_ranking = tuple(source_outcome.ranking)
    source_ranking_ids = tuple(rank.security_id for rank in source_ranking)
    if len(source_ranking_ids) != len(set(source_ranking_ids)) or set(source_ranking_ids) != set(
        command.screening.universe_security_ids
    ):
        raise ValueError("RESEARCH_SELECTION_RANKING_UNIVERSE_MISMATCH")
    source_positive_percentiles = {
        rank.security_id: rank.positive_percentile for rank in source_ranking
    }
    source_terminal_percentiles = {
        rank.security_id: rank.terminal_percentile for rank in source_ranking
    }
    if (
        source_positive_percentiles != command.screening.positive_percentiles
        or source_terminal_percentiles != command.screening.terminal_percentiles
    ):
        raise ValueError("RESEARCH_SELECTION_PERCENTILE_MISMATCH")


def _research_data_gate_results(
    command: ResearchCommand,
    error_code: str | None,
    member_runs: tuple[ResearchMemberRunResult, ...] = (),
) -> tuple[GateResult, ...]:
    """Persist each member/data-type gate when the required-facts Provider fails."""
    member_run_gate_results = tuple(
        GateResult(
            gate_id=f"RESEARCH_RUN:{member.security_id}",
            status="PASSED" if member.status == "SUCCEEDED" else "FAILED",
        )
        for member in member_runs
    )
    if error_code != "RESEARCH_DATA_UNAVAILABLE" and not (error_code or "").startswith(
        "RESEARCH_REQUIRED_FACTS_INCOMPLETE"
    ) and not member_run_gate_results:
        return ()
    detailed_manifest_member_ids = {
        member.security_id
        for member in command.members
        if any(entry.completeness != "COMPLETE" for entry in member.data_manifest.entries)
    }
    manifest_is_detailed = bool(detailed_manifest_member_ids)
    invalid_structured_member_ids = {
        member.security_id
        for member in command.members
        if all(entry.completeness == "COMPLETE" for entry in member.data_manifest.entries)
        and (
            not member.structured_facts.money_flow.is_complete
            or any(value is None for value in member.structured_signals.values())
        )
    }
    invalid_structured_data_types = {
        (member.security_id, RAW_SCORE_FEATURE_DATA_TYPES[signal_id])
        for member in command.members
        for signal_id, value in member.structured_signals.items()
        if value is None and signal_id in RAW_SCORE_FEATURE_DATA_TYPES
    }
    invalid_structured_data_types.update(
        (member.security_id, "MONEY_FLOW")
        for member in command.members
        if all(entry.completeness == "COMPLETE" for entry in member.data_manifest.entries)
        and not member.structured_facts.money_flow.is_complete
    )
    data_failure_member_ids = {
        member.security_id
        for member in member_runs
        if member.error_code == "RESEARCH_DATA_UNAVAILABLE"
        or (member.error_code or "").startswith("RESEARCH_REQUIRED_FACTS_INCOMPLETE")
    }
    unknown_data_member_ids = (
        data_failure_member_ids
        - detailed_manifest_member_ids
        - invalid_structured_member_ids
    )
    unattributed_data_failure = (
        (
            error_code == "RESEARCH_DATA_UNAVAILABLE"
            or (error_code or "").startswith("RESEARCH_REQUIRED_FACTS_INCOMPLETE")
        )
        and not manifest_is_detailed
        and not invalid_structured_member_ids
        and not data_failure_member_ids
    )
    data_gate_results = tuple(
        GateResult(
            gate_id=f"RESEARCH_DATA:{member.security_id}:{entry.data_type}",
            status=(
                "FAILED"
                if manifest_is_detailed and entry.completeness != "COMPLETE"
                else "FAILED"
                if (member.security_id, entry.data_type) in invalid_structured_data_types
                else "UNKNOWN"
                if member.security_id in unknown_data_member_ids or unattributed_data_failure
                else "PASSED"
            ),
        )
        for member in command.members
        for entry in member.data_manifest.entries
    )
    return (*member_run_gate_results, *data_gate_results)


def _raw_score_model_evidence_gate_results(command: ResearchCommand) -> tuple[GateResult, ...]:
    """Persist each frozen raw-score waterline result before the aggregate failure."""
    model = command.raw_score_model
    return (
        GateResult(
            gate_id="RAW_SCORE_MATURE_MONTHS",
            status="PASSED" if model.mature_months >= 60 else "FAILED",
        ),
        GateResult(
            gate_id="RAW_SCORE_TRAINING_RECORD_COUNT",
            status="PASSED" if model.training_record_count >= 500 else "FAILED",
        ),
        GateResult(
            gate_id="RAW_SCORE_POSITIVE_CLASS",
            status="PASSED" if model.positive_record_count >= 50 else "FAILED",
        ),
        GateResult(
            gate_id="RAW_SCORE_NEGATIVE_CLASS",
            status="PASSED" if model.negative_record_count >= 50 else "FAILED",
        ),
        GateResult(gate_id="STRUCTURED_Z20", status="FAILED"),
    )


async def execute_research_risk_journey(
    case: FrozenDecisionCase,
    *,
    execute_research: Callable[[], Awaitable[FrameworkRunResult]],
    execute_risk: Callable[[FrameworkRunResult, ResearchRiskPlan], Awaitable[FrameworkRunResult]],
    legacy: bool = False,
    historical: bool = False,
) -> FrameworkRunResult:
    """Orchestrate typed research handoff and independent risk execution in the host."""
    command = case.research
    assert command is not None
    try:
        research_run = await execute_research()
    except MappedDurableRunMissingError:
        research_run = FrameworkRunResult(
            run_id=case.framework_run_id,
            status="FAILED",
            output=None,
            error_code="RESEARCH_RUN_MISSING",
            transitions=(
                FrameworkRunTransition(
                    run_id=case.framework_run_id,
                    status="FAILED",
                    reason="RESEARCH_RUN_MISSING",
                ),
            ),
        )
    except ValueError:
        research_run = FrameworkRunResult(
            run_id=case.framework_run_id,
            status="FAILED",
            output=None,
            error_code="RESEARCH_RUN_RECOVERY_FAILED",
            transitions=(
                FrameworkRunTransition(
                    run_id=case.framework_run_id,
                    status="FAILED",
                    reason="RESEARCH_RUN_RECOVERY_FAILED",
                ),
            ),
        )
    if research_run.status != "SUCCEEDED":
        return research_run
    if research_run.output is None:
        return replace(
            research_run,
            research_validation_error_code="RESEARCH_OUTPUT_MISSING",
        )
    try:
        draft = (
            decode_legacy_research_draft(json.loads(research_run.output))
            if legacy
            else decode_historical_research_draft(json.loads(research_run.output))
            if historical
            else ResearchDraft.model_validate_json(research_run.output)
        )
    except ValueError:
        return replace(
            research_run,
            research_validation_error_code="RESEARCH_OUTPUT_INVALID",
        )
    tool_evidence = research_run.research_tool_evidence
    research_run_ids = tuple(
        member_run.run_id for member_run in research_run.research_member_runs
    )
    try:
        validate_research_draft(command, draft, tool_evidence)
    except ValueError:
        return replace(
            research_run,
            research_validation_error_code="RESEARCH_PROVENANCE_INVALID",
        )
    if command.failure_mode == "RAW_SCORE":
        return replace(
            research_run,
            output=_research_framework_output_json(
                ResearchFrameworkOutput(
                    research_run_id=research_run.run_id,
                    research_run_ids=research_run_ids,
                    risk_run_id=None,
                    draft=draft,
                    risk_veto=None,
                    tool_evidence_refs=tuple(item.evidence_id for item in tool_evidence),
                    tool_evidence=tool_evidence,
                ),
                legacy=legacy,
                historical=historical,
            ),
        )
    try:
        historical_raw_score_payloads = (
            research_outcome_raw_score_payloads(
                case.expected_external_result.research,
            )
            if legacy or historical
            else None
        )
        risk_plan = prepare_research_risk_plan(
            command,
            research_run.run_id,
            draft,
            tool_evidence,
            research_run_ids,
            legacy=legacy,
            historical=historical,
            raw_score_payloads=historical_raw_score_payloads,
        )
    except RawScoreCalculationError as error:
        return replace(
            research_run,
            output=_research_framework_output_json(
                ResearchFrameworkOutput(
                    research_run_id=research_run.run_id,
                    research_run_ids=research_run_ids,
                    risk_run_id=None,
                    draft=draft,
                    risk_veto=None,
                    tool_evidence_refs=tuple(item.evidence_id for item in tool_evidence),
                    tool_evidence=tool_evidence,
                ),
                legacy=legacy,
                historical=historical,
            ),
            raw_score_error_code=str(error),
        )
    try:
        risk_run = await execute_risk(research_run, risk_plan)
    except MappedDurableRunMissingError:
        risk_run = FrameworkRunResult(
            run_id=risk_plan.risk_run_id,
            status="FAILED",
            output=None,
            error_code="RISK_RUN_MISSING",
            transitions=(
                FrameworkRunTransition(
                    run_id=risk_plan.risk_run_id,
                    status="FAILED",
                    reason="RISK_RUN_MISSING",
                ),
            ),
        )
    except ValueError:
        risk_run = FrameworkRunResult(
            run_id=risk_plan.risk_run_id,
            status="FAILED",
            output=None,
            error_code="RISK_RUN_RECOVERY_FAILED",
            transitions=(
                FrameworkRunTransition(
                    run_id=risk_plan.risk_run_id,
                    status="FAILED",
                    reason="RISK_RUN_RECOVERY_FAILED",
                ),
            ),
        )
    if risk_run.status in {"SUCCEEDED", "REJECTED"} and risk_run.output is not None:
        try:
            risk_veto = RiskVetoDraft.model_validate_json(risk_run.output)
        except ValueError:
            risk_veto = None
    else:
        risk_veto = None
    return replace(
        research_run,
        output=_research_framework_output_json(
            ResearchFrameworkOutput(
                research_run_id=research_run.run_id,
                research_run_ids=research_run_ids,
                risk_run_id=risk_run.run_id,
                draft=draft,
                risk_veto=risk_veto,
                raw_scores=risk_plan.raw_scores,
                tool_evidence_refs=risk_plan.tool_evidence_refs,
                tool_evidence=risk_plan.tool_evidence,
                member_handoffs=risk_plan.member_handoffs,
            ),
            legacy=legacy,
            historical=historical,
        ),
        risk_run_id=risk_run.run_id,
        risk_run_status=risk_run.status,
        risk_waiting_reason=risk_run.waiting_reason,
        risk_run_error_code=risk_run.error_code,
        risk_transitions=risk_run.transitions,
        risk_transitions_durably_recorded=risk_run.transitions_durably_recorded,
    )


def _research_framework_output_json(
    output: ResearchFrameworkOutput,
    *,
    legacy: bool,
    historical: bool = False,
) -> str:
    """Serialize the framework envelope without expanding a historical contract."""
    payload = output.model_dump(
        mode="json",
        exclude={"research_run_ids"} if legacy else None,
    )
    if legacy or historical:
        payload["draft"] = research_draft_payload(
            output.draft,
            legacy=legacy,
            historical=historical,
        )
    if legacy or historical:
        payload["raw_scores"] = tuple(
            research_raw_score_payload(score, legacy=legacy)
            for score in output.raw_scores or ()
        )
    if legacy:
        payload["tool_evidence"] = tuple(
            research_evidence_payload(evidence, legacy=True) for evidence in output.tool_evidence
        )
        payload["member_handoffs"] = tuple(
            research_member_handoff_payload(handoff, legacy=True)
            for handoff in output.member_handoffs
        )
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def replay_default_frozen_decision_case(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    framework: FrozenFramework,
    business_identity: str,
    *,
    recovery_case: FrozenDecisionCase | None = None,
) -> DecisionCaseExecution:
    """Replay only when the caller names the fixture's immutable business identity."""
    if business_identity != case.business_identity:
        raise ValueError("unknown frozen decision-case business identity")
    if recovery_case is not None:
        original = recovery_case.model_copy(update={"recovery_framework_run_id": None})
        if not any(
            original.matches_legacy_recovery_input(candidate)
            or original.matches_runtime_upgrade_recovery_input(candidate)
            for candidate in (case, *case.legacy_contract_recovery_cases)
        ):
            raise ValueError("original snapshot does not match the frozen recovery input")
        asyncio.run(framework.validate_recovery(original))
        case = original.model_copy(update={"recovery_framework_run_id": original.framework_run_id})
    return _run_frozen_decision_case(case, ledger, framework)


def retry_default_frozen_decision_case_notification(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    business_identity: str,
    status: NotificationAttemptStatus,
) -> NotificationAttempt:
    """Record one recoverable synthetic notification outcome for an existing report."""
    if business_identity != case.business_identity:
        raise ValueError("unknown frozen decision-case business identity")
    with ledger.serialize_case_execution() as connection:
        business_object_id = ledger.resolve_business_object_id(case, connection)
        event = ledger.get_original_decision_event(business_object_id, connection)
        report = ledger.get_original_formal_report(business_object_id, connection)
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
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    business_identity: str,
) -> DecisionCaseCorrection:
    """Append the D0 correction fact without replacing the original report."""
    if business_identity != case.business_identity:
        raise ValueError("unknown frozen decision-case business identity")
    correction_error: DecisionEventCommitError | None = None
    publication_error: DecisionEventCommitError | None = None
    report: FormalReport | None = None
    with ledger.serialize_case_execution() as connection:
        business_object_id = ledger.resolve_business_object_id(case, connection)
        original_event = ledger.get_original_decision_event(
            business_object_id,
            connection,
        )
        original_report = ledger.get_original_formal_report(
            business_object_id,
            connection,
        )
        if original_event is None or original_report is None:
            raise ValueError("a published original report is required before correction")
        correction_event = ledger.get_correction_event(
            original_event.decision_event_id,
            connection,
        )
        if correction_event is None:
            correction_case = _correction_case(original_event.case, case)
            correction_event_id = correction_case.correction_event_id(
                original_event.decision_event_id
            )
            correction_result = original_event.result.model_copy(
                update={
                    "outcome_code": "SYNTHETIC_CORRECTION_RECORDED",
                    "summary": (
                        "Synthetic D0 correction recorded without replacing "
                        "the original decision event."
                    ),
                    "key_reasons": (
                        "The original completion statement is corrected "
                        "to an incomplete checklist.",
                        "The original evidence cutoff and formal report remain available.",
                    ),
                    "correction_evidence": CorrectionEvidence.model_validate(
                        load_frozen_correction_payload()
                    ),
                }
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
            correction_written_at = ledger.observed_at()
            attempted_fact = ledger.build_event_fact(
                case=correction_case,
                framework_run_id=original_event.framework_run_id,
                result=correction_result,
                stage_results=correction_stages,
                decision_event_id=correction_event_id,
                corrects_event_id=original_event.decision_event_id,
                committed_at=correction_written_at,
                generated_at=correction_written_at,
            )
            try:
                correction_event = ledger.commit_event_fact(connection, attempted_fact)
            except DecisionEventCommitUncertainError as error:
                correction_event = ledger.reconcile_event_commit(connection, attempted_fact)
                if correction_event is None:
                    _record_correction_commit_failure(
                        ledger,
                        connection,
                        correction_case,
                        correction_event_id,
                        original_event.framework_run_id,
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


def _run_frozen_decision_case(
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    framework_adapter: FrozenFramework,
) -> DecisionCaseExecution:
    """Run or replay one exact frozen business identity without HTTP transport."""
    with ledger.serialize_case_execution() as connection:
        existing_business_object_id = ledger.resolve_business_object_id(case, connection)
        has_existing_mapping = (
            ledger.get_business_object_mapping(existing_business_object_id, connection) is not None
        )
        if (
            case.monitoring is not None or case.universe is not None or case.selection is not None
        ) and has_existing_mapping:
            mapping = ledger.get_business_object_mapping(existing_business_object_id, connection)
            if mapping is not None and mapping.case is not None:
                case = mapping.case
        known_run_ids = ledger.mapped_framework_run_ids(connection)
    if not has_existing_mapping and case.recovery_framework_run_id is None:
        try:
            legacy_case = asyncio.run(framework_adapter.recover_unmapped(case, known_run_ids))
        except ValueError as error:
            raise DecisionEventCommitError("durable legacy framework recovery failed") from error
        if legacy_case is not None:
            case = legacy_case
        elif case.governance is not None:
            validate_new_request(case.governance)
    business_object_id = ledger.persist_business_mapping_before_framework(case)
    with ledger.serialize_case_execution() as connection:
        existing_report = ledger.get_original_formal_report(
            business_object_id,
            connection,
        )
        if existing_report is not None:
            return _published_execution(existing_report)
        fact = ledger.get_original_decision_event(business_object_id, connection)

    if fact is None:
        with _serialize_local_framework_execution(business_object_id):
            with ledger.serialize_case_execution() as connection:
                existing_report = ledger.get_original_formal_report(
                    business_object_id,
                    connection,
                )
                if existing_report is not None:
                    return _published_execution(existing_report)
                fact = ledger.get_original_decision_event(business_object_id, connection)
                if fact is None:
                    mapping = ledger.get_business_object_mapping(
                        business_object_id,
                        connection,
                    )
                    if mapping is None:
                        raise RuntimeError(
                            "durable business mapping is missing before framework execution"
                        )
                    execution_case = mapping.case or _snapshotless_recovery_case(
                        case,
                        business_object_id,
                        mapping,
                    )
                    ledger.ensure_business_object(connection, execution_case)
                    if _has_durable_framework_history(
                        ledger.get_stage_results(business_object_id, connection)
                    ):
                        execution_case = execution_case.model_copy(
                            update={"recovery_framework_run_id": mapping.framework_run_id}
                        )
                    if execution_case.research is not None:
                        try:
                            _validate_research_selection_event(
                                execution_case,
                                ledger.get_decision_event(
                                    execution_case.research.selection_event_id,
                                    connection,
                                ),
                            )
                        except ValueError as error:
                            ledger.record_stage_result(
                                connection,
                                case=execution_case,
                                stage_result=StageResult(
                                    phase="RESEARCH",
                                    status="FAILED",
                                    gate_results=(
                                        GateResult(
                                            gate_id="RESEARCH_SELECTION_EVENT",
                                            status="FAILED",
                                        ),
                                    ),
                                    reasons=(str(error),),
                                ),
                                framework_run_id=execution_case.framework_run_id,
                            )
                            ledger.record_stage_result(
                                connection,
                                case=execution_case,
                                stage_result=_failed_host_validation(str(error)),
                                framework_run_id=execution_case.framework_run_id,
                            )
                            return _unpublished_execution(
                                execution_case,
                                framework_run_status="CREATED",
                                business_result_status=None,
                                business_lifecycle=None,
                                business_commit_status="NOT_ATTEMPTED",
                                stage_results=ledger.get_stage_results(
                                    execution_case.business_object_id,
                                    connection,
                                ),
                            )
                else:
                    execution_case = None
            if fact is None:
                assert execution_case is not None
                try:

                    async def record_transition(transition: FrameworkRunTransition) -> None:
                        await _record_framework_transition(
                            ledger,
                            execution_case,
                            transition,
                        )

                    async def record_auxiliary_run_reservation(risk_run_id: str) -> bool:
                        return await _record_auxiliary_run_reservation(
                            ledger,
                            execution_case,
                            risk_run_id,
                        )

                    async def record_research_member_run_reservation(run_id: str) -> bool:
                        return await _record_research_member_run_reservation(
                            ledger,
                            execution_case,
                            run_id,
                        )

                    if execution_case.research is not None:

                        async def execute_risk(
                            research_run: FrameworkRunResult,
                            risk_plan: ResearchRiskPlan,
                        ) -> FrameworkRunResult:
                            async def record_risk_transition(
                                transition: FrameworkRunTransition,
                            ) -> None:
                                await _record_framework_transition(
                                    ledger,
                                    execution_case,
                                    transition,
                                    framework_run_id=risk_plan.risk_run_id,
                                    phase="RISK_FRAMEWORK_RUN",
                                )

                            return await framework_adapter.execute_research_risk_run(
                                execution_case,
                                research_run,
                                risk_plan,
                                record_risk_transition,
                                record_auxiliary_run_reservation=(record_auxiliary_run_reservation),
                            )

                        framework = asyncio.run(
                            execute_research_risk_journey(
                                execution_case,
                                execute_research=lambda: framework_adapter.execute_research_run(
                                    execution_case,
                                    record_transition,
                                    record_member_run_reservation=(
                                        record_research_member_run_reservation
                                    ),
                                ),
                                execute_risk=execute_risk,
                                legacy=research_contract_mode_for_case(execution_case) == "legacy",
                                historical=(
                                    research_contract_mode_for_case(execution_case)
                                    == "historical"
                                ),
                            )
                        )
                    else:
                        framework = asyncio.run(
                            framework_adapter.execute(
                                execution_case,
                                record_transition,
                                record_auxiliary_run_reservation=(record_auxiliary_run_reservation),
                            )
                        )
                except MappedDurableRunMissingError as error:
                    raise DecisionEventCommitError(
                        "business identity maps to a missing durable framework Run; "
                        "durable framework recovery failed"
                    ) from error
                except ValueError as error:
                    raise DecisionEventCommitError("durable framework recovery failed") from error
                with ledger.serialize_case_execution() as connection:
                    existing_report = ledger.get_original_formal_report(
                        business_object_id,
                        connection,
                    )
                    if existing_report is not None:
                        return _published_execution(existing_report)
                    fact = ledger.get_original_decision_event(
                        business_object_id,
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
            business_object_id,
            connection,
        )
        if existing_report is not None:
            return _published_execution(existing_report)
        current_fact = ledger.get_original_decision_event(business_object_id, connection)
        if current_fact is None:
            raise RuntimeError("committed decision event is missing before publication")
        return _publish_committed_fact(ledger, connection, current_fact)


async def _record_framework_transition(
    ledger: DecisionLedger[Transaction],
    case: FrozenDecisionCase,
    transition: FrameworkRunTransition,
    *,
    framework_run_id: str | None = None,
    phase: Literal["FRAMEWORK_RUN", "RISK_FRAMEWORK_RUN"] = "FRAMEWORK_RUN",
) -> None:
    """Commit each already-durable M-Agent transition before more framework work begins."""
    with ledger.serialize_case_execution() as connection:
        ledger.record_stage_result(
            connection,
            case=case,
            stage_result=_framework_transition_stage_result(transition, phase=phase),
            framework_run_id=framework_run_id or transition.run_id or case.framework_run_id,
            allow_repeated_occurrence=transition.status in {"RUNNING", "WAITING"},
        )


async def _record_auxiliary_run_reservation(
    ledger: DecisionLedger[Transaction],
    case: FrozenDecisionCase,
    framework_run_id: str,
) -> bool:
    """Reserve a deterministic auxiliary Run identity before creating it."""
    with ledger.serialize_case_execution() as connection:
        history = ledger.get_stage_results(
            case.business_object_id,
            connection,
            framework_run_id=framework_run_id,
        )
        if any(stage_result.phase == "RISK_FRAMEWORK_RUN" for stage_result in history):
            return False
        if any(stage_result.phase == "AUXILIARY_RUN_RESERVATION" for stage_result in history):
            return True
        ledger.record_stage_result(
            connection,
            case=case,
            stage_result=StageResult(
                phase="AUXILIARY_RUN_RESERVATION",
                status="PENDING",
                gate_results=(GateResult(gate_id="RISK_RUN_RESERVED", status="PASSED"),),
                reasons=("RISK_RUN_ID_RESERVED",),
            ),
            framework_run_id=framework_run_id,
        )
    return True


async def _record_research_member_run_reservation(
    ledger: DecisionLedger[Transaction],
    case: FrozenDecisionCase,
    framework_run_id: str,
) -> bool:
    """Reserve one member Run without allowing replacement after execution began."""
    with ledger.serialize_case_execution() as connection:
        history = ledger.get_stage_results(
            case.business_object_id,
            connection,
            framework_run_id=framework_run_id,
        )
        if any(stage_result.phase == "FRAMEWORK_RUN" for stage_result in history):
            return False
        if any(
            stage_result.phase == "AUXILIARY_RUN_RESERVATION"
            and any(
                gate.gate_id == "RESEARCH_MEMBER_RUN_RESERVED"
                for gate in stage_result.gate_results
            )
            for stage_result in history
        ):
            return True
        if any(stage_result.phase == "AUXILIARY_RUN_RESERVATION" for stage_result in history):
            return False
        ledger.record_stage_result(
            connection,
            case=case,
            stage_result=StageResult(
                phase="AUXILIARY_RUN_RESERVATION",
                status="PENDING",
                gate_results=(
                    GateResult(gate_id="RESEARCH_MEMBER_RUN_RESERVED", status="PASSED"),
                ),
                reasons=("RESEARCH_MEMBER_RUN_ID_RESERVED",),
            ),
            framework_run_id=framework_run_id,
        )
    return True


@contextmanager
def _serialize_local_framework_execution(business_object_id: str) -> Iterator[None]:
    """Prevent same-process replays from racing one durable M-Agent Run lease."""
    with _FRAMEWORK_EXECUTION_LOCKS_GUARD:
        lock = _FRAMEWORK_EXECUTION_LOCKS.setdefault(business_object_id, Lock())
    with lock:
        yield


def _has_durable_framework_history(stage_results: tuple[StageResult, ...]) -> bool:
    """Require the persisted M-Agent Run once the host observed a transition."""
    return any(stage_result.phase == "FRAMEWORK_RUN" for stage_result in stage_results)


def _snapshotless_recovery_case(
    current_case: FrozenDecisionCase,
    business_object_id: str,
    mapping: BusinessObjectMapping,
) -> FrozenDecisionCase:
    """Recover a pre-snapshot mapping only through its original durable Run."""
    candidates = (
        current_case,
        current_case.legacy_projection_recovery_case(mapping.framework_run_id),
        *current_case.legacy_contract_recovery_cases,
    )
    for candidate in candidates:
        if (
            candidate.business_object_id == business_object_id
            and candidate.case_id == mapping.case_id
            and candidate.frozen_input_fingerprint == mapping.frozen_input_fingerprint
            and candidate.matches_legacy_recovery_input()
        ):
            return candidate.model_copy(
                update={"recovery_framework_run_id": mapping.framework_run_id}
            )
    raise DecisionEventCommitError(
        "durable business mapping lacks a frozen case snapshot for this build"
    )


def _commit_framework_result(
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    execution_case: FrozenDecisionCase,
    framework: FrameworkRunResult,
) -> DecisionEventFact | DecisionCaseExecution:
    """Validate one terminal framework result and append its host business fact."""
    framework_stage_results = _framework_stage_results(execution_case, framework)
    durable_transition_count = (
        len(framework.transitions) if framework.transitions_durably_recorded else 0
    )
    _record_framework_stage_results(
        ledger,
        connection,
        execution_case,
        execution_case.framework_run_id,
        framework_stage_results,
        durable_transition_count,
    )
    if execution_case.research is not None:
        return _commit_research_framework_result(
            ledger,
            connection,
            execution_case,
            framework,
            framework_stage_results,
            durable_transition_count,
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
    qualification_result: StageResult | None = None
    portfolio_result: StageResult | None = None
    position_result: StageResult | None = None
    concentration_result: StageResult | None = None
    drawdown_result: StageResult | None = None
    liquidity_result: StageResult | None = None
    stress_result: StageResult | None = None
    execution_plan_result: StageResult | None = None
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
            if business_result is not None and execution_case.governance is not None:
                assert execution_case.access_scope is not None
                governance = adjudicate(
                    execution_case.governance,
                    event_id=execution_case.decision_event_id,
                    observed_at=ledger.observed_at(),
                    knowledge_cutoff=execution_case.knowledge_cutoff,
                    history=ledger.governance_history(connection, execution_case.access_scope),
                    business_prerequisite_met=business_result.status == "SUCCEEDED",
                )
                result = result.model_copy(update={"governance": governance})
                qualification_result = StageResult(
                    phase="QUALIFICATION",
                    status="SUCCEEDED" if governance.disposition == "APPROVED" else "REJECTED",
                    gate_results=(
                        GateResult(
                            gate_id="SCOPED_QUALIFICATION",
                            status="PASSED" if governance.disposition == "APPROVED" else "FAILED",
                        ),
                    ),
                    reasons=governance.reasons,
                )
            portfolio_command = (
                execution_case.concentration.authorization
                if execution_case.concentration is not None
                else execution_case.portfolio
            )
            portfolio_history: tuple[PortfolioAuthorizationOutcome, ...] = ()
            if business_result is not None and portfolio_command is not None:
                assert execution_case.access_scope is not None
                portfolio_history = ledger.portfolio_authorization_history(
                    connection,
                    execution_case.access_scope,
                    portfolio_id_for(portfolio_command),
                    execution_case.knowledge_cutoff,
                )
                portfolio = adjudicate_portfolio(
                    portfolio_command,
                    event_id=execution_case.decision_event_id,
                    observed_at=ledger.observed_at(),
                    history=portfolio_history,
                    lineage_history=ledger.portfolio_authorization_lineage(
                        connection,
                        execution_case.access_scope,
                        portfolio_id_for(portfolio_command),
                    ),
                    owner_lineage_history=ledger.portfolio_authorization_owner_lineage(
                        connection,
                        execution_case.access_scope,
                    ),
                    access_account_ids=execution_case.access_scope.account_ids,
                    knowledge_cutoff=execution_case.knowledge_cutoff,
                    business_prerequisite_met=business_result.status == "SUCCEEDED",
                    stress_history=ledger.portfolio_stress_history(
                        connection,
                        execution_case.access_scope,
                        portfolio_id_for(portfolio_command),
                    ),
                )
                result = result.model_copy(update={"portfolio": portfolio})
                portfolio_result = StageResult(
                    phase="PORTFOLIO_AUTHORIZATION",
                    status="SUCCEEDED" if portfolio.disposition != "DENIED" else "REJECTED",
                    gate_results=(
                        GateResult(
                            gate_id="PORTFOLIO_SCOPE",
                            status="PASSED" if portfolio.disposition != "DENIED" else "FAILED",
                        ),
                    ),
                    reasons=portfolio.reasons,
                )
            position_command = (
                execution_case.concentration.position_snapshot
                if execution_case.concentration is not None
                else execution_case.position
                or (execution_case.stress.position_snapshot if execution_case.stress else None)
            )
            if business_result is not None and position_command is not None:
                assert execution_case.access_scope is not None
                position = reconcile_position(
                    position_command,
                    prior_ledger=ledger.position_ledger_history(
                        connection,
                        execution_case.access_scope,
                        position_command.cutoff_at,
                    ),
                    prior_cash_states=ledger.position_cash_history(
                        connection,
                        execution_case.access_scope,
                        position_command.cutoff_at,
                    ),
                )
                result = result.model_copy(update={"position": position})
                position_result = StageResult(
                    phase="POSITION_RECONCILIATION",
                    status=("SUCCEEDED" if position.disposition == "RECONCILED" else "REJECTED"),
                    gate_results=(
                        GateResult(
                            gate_id="AUTHORITATIVE_POSITION_FACTS",
                            status=("PASSED" if position.disposition == "RECONCILED" else "FAILED"),
                        ),
                    ),
                    reasons=position.reasons,
                )
            if business_result is not None and execution_case.drawdown is not None:
                assert execution_case.access_scope is not None
                drawdown_command = execution_case.drawdown
                drawdown = adjudicate_drawdown(
                    drawdown_command,
                    event_id=execution_case.decision_event_id,
                    account_ids=execution_case.access_scope.account_ids,
                    authorizations=ledger.portfolio_authorization_lineage(
                        connection, execution_case.access_scope, drawdown_command.portfolio_id
                    ),
                    position=ledger.position_evidence_for_drawdown(
                        connection,
                        execution_case.access_scope,
                        drawdown_command.valuation.position_event_id,
                        drawdown_command.cutoff_at,
                    ),
                    history=ledger.drawdown_history(connection, execution_case.access_scope),
                    before_positions={
                        flow.before_valuation.position_event_id: (
                            ledger.position_evidence_for_drawdown(
                                connection,
                                execution_case.access_scope,
                                flow.before_valuation.position_event_id,
                                drawdown_command.cutoff_at,
                            )
                        )
                        for flow in drawdown_command.capital_flows
                    },
                    business_prerequisite_met=business_result.status == "SUCCEEDED",
                )
                result = result.model_copy(update={"drawdown": drawdown})
                drawdown_result = StageResult(
                    phase="DRAWDOWN_PROTECTION",
                    status=(
                        "SUCCEEDED"
                        if drawdown.disposition == "ACCEPTED"
                        else "UNKNOWN"
                        if drawdown.disposition == "UNKNOWN"
                        else "REJECTED"
                    ),
                    gate_results=(),
                    reasons=drawdown.reasons,
                )
            if business_result is not None and execution_case.stress is not None:
                stress_command = execution_case.stress
                scope = execution_case.access_scope
                assert scope is not None
                authorization = adjudicate_portfolio(
                    PortfolioUseCommand(
                        operation="PORTFOLIO_USE",
                        portfolio_id=stress_command.portfolio_id,
                        authorization_id=stress_command.authorization_id,
                        requested_action="DETERMINISTIC_PROTECTION",
                    ),
                    event_id=execution_case.decision_event_id,
                    observed_at=ledger.observed_at(),
                    history=ledger.portfolio_authorization_history(
                        connection,
                        scope,
                        stress_command.portfolio_id,
                        execution_case.knowledge_cutoff,
                    ),
                    lineage_history=ledger.portfolio_authorization_lineage(
                        connection, scope, stress_command.portfolio_id
                    ),
                    owner_lineage_history=ledger.portfolio_authorization_owner_lineage(
                        connection, scope
                    ),
                    access_account_ids=scope.account_ids,
                    knowledge_cutoff=execution_case.knowledge_cutoff,
                    business_prerequisite_met=True,
                    require_current_authorization=True,
                )
                stress = assess_stress(
                    stress_command,
                    authorization,
                    position,
                    history=ledger.portfolio_stress_history(
                        connection, scope, stress_command.portfolio_id
                    ),
                )
                result = result.model_copy(update={"stress": stress})
                stress_result = StageResult(
                    phase="PORTFOLIO_STRESS",
                    status="REJECTED" if stress.state == "UNKNOWN" else "SUCCEEDED",
                    gate_results=(
                        GateResult(
                            gate_id="GROSS_STRESS_EVIDENCE",
                            status="UNKNOWN" if stress.state == "UNKNOWN" else "PASSED",
                        ),
                    ),
                    reasons=stress.reasons,
                )
            if business_result is not None and execution_case.liquidity is not None:
                command = execution_case.liquidity
                scope = execution_case.access_scope
                assert scope is not None
                authorization_history = ledger.portfolio_authorization_history(
                    connection, scope, command.portfolio_id, execution_case.knowledge_cutoff
                )
                authorization_lineage = ledger.portfolio_authorization_lineage(
                    connection, scope, command.portfolio_id
                )
                protection_request = PortfolioUseCommand(
                    operation="PORTFOLIO_USE",
                    portfolio_id=command.portfolio_id,
                    authorization_id=command.authorization_id,
                    requested_action="DETERMINISTIC_PROTECTION",
                )
                portfolio, purchase_authorization = (
                    adjudicate_portfolio(
                        request,
                        event_id=execution_case.decision_event_id,
                        observed_at=execution_case.knowledge_cutoff,
                        knowledge_cutoff=execution_case.knowledge_cutoff,
                        history=authorization_history,
                        lineage_history=authorization_lineage,
                        access_account_ids=scope.account_ids,
                        business_prerequisite_met=business_result.status == "SUCCEEDED",
                    )
                    for request in (
                        protection_request,
                        protection_request.model_copy(update={"requested_action": "NEW_EXPOSURE"}),
                    )
                )
                position = reconcile_position(
                    command.position_snapshot,
                    prior_ledger=ledger.position_ledger_history(
                        connection,
                        scope,
                        command.position_snapshot.cutoff_at,
                    ),
                    prior_cash_states=ledger.position_cash_history(
                        connection,
                        scope,
                        command.position_snapshot.cutoff_at,
                    ),
                )
                liquidity = assess_liquidity(
                    command,
                    portfolio,
                    position,
                    purchase_authorization=purchase_authorization,
                    event_id=execution_case.decision_event_id,
                    history=ledger.liquidity_history(
                        connection,
                        scope,
                        command.portfolio_id,
                        command.position_snapshot.cutoff_at,
                    ),
                )
                result = result.model_copy(update={"liquidity": liquidity, "position": position})
                liquidity_result = StageResult(
                    phase="LIQUIDITY_PROTECTION",
                    status="REJECTED"
                    if liquidity.disposition == "EVIDENCE_FAILED"
                    else "SUCCEEDED",
                    gate_results=(
                        GateResult(
                            gate_id="LIQUIDITY_EVIDENCE",
                            status="FAILED"
                            if liquidity.disposition == "EVIDENCE_FAILED"
                            else "PASSED",
                        ),
                    ),
                    reasons=liquidity.reasons,
                )
            if business_result is not None and execution_case.concentration is not None:
                assert result.portfolio is not None and result.position is not None
                assert execution_case.access_scope is not None
                concentration = assess_concentration(
                    execution_case.concentration,
                    result.portfolio,
                    result.position,
                    authorization_history=portfolio_history,
                    history=ledger.concentration_history(
                        connection,
                        execution_case.access_scope,
                        execution_case.concentration.authorization.portfolio_id,
                    ),
                    authorization_lineage=ledger.portfolio_authorization_lineage(
                        connection,
                        execution_case.access_scope,
                        execution_case.concentration.authorization.portfolio_id,
                    ),
                )
                result = result.model_copy(update={"concentration": concentration})
                concentration_result = StageResult(
                    phase="ISSUER_CONCENTRATION",
                    status="SUCCEEDED" if concentration.disposition == "ASSESSED" else "REJECTED",
                    gate_results=(
                        GateResult(
                            gate_id="CONCENTRATION_FACTS",
                            status="PASSED"
                            if concentration.disposition == "ASSESSED"
                            else "FAILED",
                        ),
                    ),
                    reasons=concentration.reasons,
                )
            if business_result is not None and execution_case.execution_plan is not None:
                plan = adjudicate_execution_plan(execution_case, ledger, connection)
                result = result.model_copy(update={"execution_plan": plan})
                execution_plan_result = StageResult(
                    phase="EXECUTION_PLAN",
                    status="REJECTED" if plan.disposition == "BLOCKED" else "SUCCEEDED",
                    gate_results=(),
                    reasons=plan.reasons,
                )
            if business_result is not None and execution_case.monitoring is not None:
                result = result.model_copy(
                    update={"monitoring": assess_monitoring(execution_case, ledger, connection)}
                )
            if business_result is not None and execution_case.universe is not None:
                assert execution_case.access_scope is not None
                universe = freeze_universe(
                    execution_case.universe,
                    history=ledger.governance_history(connection, execution_case.access_scope),
                    business_prerequisite_met=business_result.status == "SUCCEEDED",
                )
                result = result.model_copy(
                    update={
                        "universe": universe,
                        "outcome_code": f"UNIVERSE_{universe.disposition}",
                        "summary": "Synthetic monthly universe decision.",
                        "key_reasons": universe.reasons or ("UNIVERSE_FROZEN",),
                    }
                )
                business_result = StageResult(
                    phase="BUSINESS_DECISION",
                    status="FAILED"
                    if universe.disposition == "DATA_FAILED"
                    else "REJECTED"
                    if universe.disposition == "BLOCKED"
                    else "SUCCEEDED",
                    gate_results=(
                        GateResult(
                            gate_id="UNIVERSE_DATA",
                            status="PASSED" if universe.disposition == "FROZEN" else "FAILED",
                        ),
                    ),
                    reasons=universe.reasons,
                )
                original_business_result = business_outcome_result(
                    execution_case.expected_external_result
                )
                if original_business_result.status != "SUCCEEDED":
                    business_result = original_business_result
                    result = execution_case.expected_external_result.model_copy(
                        update={"universe": universe}
                    )
            if business_result is not None and execution_case.selection is not None:
                selection_command = execution_case.selection
                source_event = ledger.get_original_decision_event(
                    selection_command.universe_object_id, connection
                )
                source_valid = (
                    source_event is not None
                    and source_event.decision_event_id == selection_command.universe_event_id
                    and source_event.case.access_scope is not None
                    and execution_case.access_scope is not None
                    and source_event.case.access_scope.same_scope_as(execution_case.access_scope)
                )
                selection = freeze_selection(
                    selection_command,
                    source_event.case.universe
                    if source_event is not None and source_valid
                    else None,
                    source_event.result.universe
                    if source_event is not None and source_valid
                    else None,
                    prerequisite=business_result.status,
                )
                result = result.model_copy(
                    update={
                        "selection": selection,
                        "outcome_code": f"SELECTION_{selection.disposition}",
                        "summary": "Synthetic monthly selection decision.",
                        "key_reasons": selection.reasons or ("SELECTION_FROZEN",),
                    }
                )
                original_selection_prerequisite = business_result
                business_result = StageResult(
                    phase="BUSINESS_DECISION",
                    status="FAILED"
                    if selection.disposition in {"DATA_FAILED", "SYSTEM_FAILED"}
                    else "ABSTAINED"
                    if selection.disposition == "ABSTAINED"
                    else "REJECTED"
                    if selection.disposition == "BLOCKED"
                    else "SUCCEEDED",
                    gate_results=(),
                    reasons=selection.reasons,
                )
                if original_selection_prerequisite.status != "SUCCEEDED":
                    business_result = original_selection_prerequisite
                    result = execution_case.expected_external_result.model_copy(
                        update={"selection": selection}
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
    if qualification_result is not None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=qualification_result,
            framework_run_id=execution_case.framework_run_id,
        )
    if portfolio_result is not None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=portfolio_result,
            framework_run_id=execution_case.framework_run_id,
        )
    if position_result is not None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=position_result,
            framework_run_id=execution_case.framework_run_id,
        )
    if concentration_result is not None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=concentration_result,
            framework_run_id=execution_case.framework_run_id,
        )
    if drawdown_result is not None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=drawdown_result,
            framework_run_id=execution_case.framework_run_id,
        )
    if liquidity_result is not None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=liquidity_result,
            framework_run_id=execution_case.framework_run_id,
        )
    if stress_result is not None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=stress_result,
            framework_run_id=execution_case.framework_run_id,
        )
    if execution_plan_result is not None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=execution_plan_result,
            framework_run_id=execution_case.framework_run_id,
        )
    framework_stage_results_before_commit = framework_stage_results[durable_transition_count:]
    current_stage_results_before_commit = (
        *framework_stage_results_before_commit,
        validation_result,
        *((business_result,) if business_result is not None else ()),
        *((qualification_result,) if qualification_result is not None else ()),
        *((portfolio_result,) if portfolio_result is not None else ()),
        *((position_result,) if position_result is not None else ()),
        *((concentration_result,) if concentration_result is not None else ()),
        *((drawdown_result,) if drawdown_result is not None else ()),
        *((liquidity_result,) if liquidity_result is not None else ()),
        *((stress_result,) if stress_result is not None else ()),
        *((execution_plan_result,) if execution_plan_result is not None else ()),
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
    stage_results = (
        *stage_results_before_commit,
        StageResult(
            phase="BUSINESS_COMMIT",
            status="SUCCEEDED",
            gate_results=(GateResult(gate_id="HOST_RESULT_SAVED", status="PASSED"),),
            reasons=(),
        ),
    )
    committed_at = ledger.observed_at()
    attempted_fact = ledger.build_event_fact(
        case=execution_case,
        framework_run_id=framework.run_id,
        result=result,
        stage_results=stage_results,
        committed_at=committed_at,
        generated_at=execution_case.report_generated_at,
    )
    try:
        return ledger.commit_event_fact(connection, attempted_fact)
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
            business_lifecycle=(
                business_lifecycle_from_stage(business_result)
                or business_lifecycle_from_stage(uncertain_commit)
            ),
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
            business_lifecycle=business_lifecycle_from_stage(business_result),
            business_commit_status="FAILED",
            stage_results=ledger.get_stage_results(execution_case.business_object_id, connection),
        )


def _commit_research_framework_result(
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    execution_case: FrozenDecisionCase,
    framework: FrameworkRunResult,
    framework_stage_results: tuple[StageResult, ...],
    durable_transition_count: int,
) -> DecisionEventFact | DecisionCaseExecution:
    """Validate the staged research envelope before committing its host result."""
    command = execution_case.research
    assert command is not None
    risk_run_id: str | None = None
    risk_stage_results: tuple[StageResult, ...] = ()
    risk_stage_results_to_restore: tuple[StageResult, ...] = ()
    risk_durable_transition_count = 0
    if framework.risk_run_id is not None:
        risk_run_id = framework.risk_run_id
        risk_stage_results = _framework_stage_results_for_auxiliary_run(framework)
        risk_durable_transition_count = (
            len(framework.risk_transitions) if framework.risk_transitions_durably_recorded else 0
        )
        risk_stage_results_to_restore = risk_stage_results[risk_durable_transition_count:]
        _record_framework_stage_results(
            ledger,
            connection,
            execution_case,
            risk_run_id,
            risk_stage_results,
            risk_durable_transition_count,
        )

    def record(stage_result: StageResult) -> None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=stage_result,
            framework_run_id=execution_case.framework_run_id,
            allow_repeated_occurrence=stage_result.status
            not in {"SUCCEEDED", "REJECTED", "ABSTAINED"},
        )

    def closed(
        *,
        validation: StageResult | None = None,
        research: StageResult | None = None,
        raw_score: StageResult | None = None,
        risk: StageResult | None = None,
    ) -> DecisionCaseExecution:
        for stage_result in (research, raw_score, risk, validation):
            if stage_result is not None:
                record(stage_result)
        return _unpublished_execution(
            execution_case,
            framework_run_status=framework.status,
            business_result_status=None,
            business_lifecycle=None,
            business_commit_status="NOT_ATTEMPTED",
            stage_results=ledger.get_stage_results(execution_case.business_object_id, connection),
        )

    if framework.run_id != execution_case.framework_run_id and framework.status not in {
        "FAILED",
        "WAITING",
    }:
        return _unpublished_execution(
            execution_case,
            framework_run_status=framework_run_status_from_stage(framework_stage_results[-1]),
            business_result_status=None,
            business_lifecycle=None,
            business_commit_status="NOT_ATTEMPTED",
            stage_results=ledger.get_stage_results(execution_case.business_object_id, connection),
        )
    if framework.status == "WAITING":
        return closed()
    if framework.status != "SUCCEEDED":
        return closed(
            research=StageResult(
                phase="RESEARCH",
                status="FAILED",
                gate_results=(
                    GateResult(gate_id="RESEARCH_RUN", status="FAILED"),
                    *_research_data_gate_results(
                        command,
                        framework.error_code,
                        framework.research_member_runs,
                    ),
                ),
                reasons=(
                    framework.error_code or framework.waiting_reason or "RESEARCH_RUN_FAILED",
                    *(
                        f"{member.security_id}:{member.error_code or member.status}"
                        for member in framework.research_member_runs
                        if member.status != "SUCCEEDED"
                    ),
                ),
            )
        )
    if framework.research_validation_error_code is not None:
        return closed(
            validation=_failed_host_validation(framework.research_validation_error_code),
            research=StageResult(
                phase="RESEARCH",
                status="FAILED",
                gate_results=(GateResult(gate_id="RESEARCH_HOST_VALIDATION", status="FAILED"),),
                reasons=(framework.research_validation_error_code,),
            ),
        )
    if framework.output is None:
        return closed(
            research=StageResult(
                phase="RESEARCH",
                status="FAILED",
                gate_results=(GateResult(gate_id="RESEARCH_OUTPUT", status="FAILED"),),
                reasons=("RESEARCH_OUTPUT_MISSING",),
            )
        )
    research_mode = research_contract_mode_for_case(execution_case) or "current"
    try:
        envelope = (
            decode_legacy_research_framework_output(json.loads(framework.output))
            if research_mode == "legacy"
            else decode_historical_research_framework_output(json.loads(framework.output))
            if research_mode == "historical"
            else ResearchFrameworkOutput.model_validate_json(framework.output)
        )
    except (ValidationError, json.JSONDecodeError, ValueError):
        return closed(
            validation=_failed_host_validation("RESEARCH_OUTPUT_CONTRACT_INVALID"),
            research=StageResult(
                phase="RESEARCH",
                status="FAILED",
                gate_results=(GateResult(gate_id="OUTPUT_CONTRACT", status="FAILED"),),
                reasons=("RESEARCH_OUTPUT_CONTRACT_INVALID",),
            ),
        )
    research_stage = StageResult(
        phase="RESEARCH",
        status="SUCCEEDED",
        gate_results=(
            GateResult(gate_id="RESEARCH_DRAFT_TYPED", status="PASSED"),
            GateResult(gate_id="RESEARCH_PROVENANCE", status="PASSED"),
        ),
        reasons=("RESEARCH_DRAFT_READY",),
    )
    if command.failure_mode == "RAW_SCORE":
        return closed(
            research=research_stage,
            raw_score=StageResult(
                phase="RAW_SCORE",
                status="FAILED",
                gate_results=(GateResult(gate_id="RAW_SCORE_AVAILABLE", status="FAILED"),),
                reasons=("RAW_SCORE_NOT_AVAILABLE",),
            ),
            validation=_failed_host_validation("RAW_SCORE_NOT_AVAILABLE"),
        )
    if framework.raw_score_error_code is not None:
        raw_score_gate_results = (
            _raw_score_model_evidence_gate_results(command)
            if framework.raw_score_error_code == "RAW_SCORE_MODEL_EVIDENCE_INSUFFICIENT"
            else (GateResult(gate_id="STRUCTURED_Z20", status="FAILED"),)
        )
        return closed(
            research=research_stage,
            raw_score=StageResult(
                phase="RAW_SCORE",
                status="FAILED",
                gate_results=raw_score_gate_results,
                reasons=(framework.raw_score_error_code,),
            ),
            validation=_failed_host_validation(framework.raw_score_error_code),
        )
    raw_score_stage = StageResult(
        phase="RAW_SCORE",
        status="SUCCEEDED",
        gate_results=(GateResult(gate_id="STRUCTURED_Z20", status="PASSED"),),
        reasons=("RAW_SCORE_FROZEN",),
    )
    if framework.risk_run_status == "WAITING":
        return closed(
            research=research_stage,
            raw_score=raw_score_stage,
        )
    if (
        envelope.risk_veto is None
        or framework.risk_run_status not in {"SUCCEEDED", "REJECTED"}
        or (
            framework.risk_run_status == "REJECTED" and envelope.risk_veto.disposition != "REJECTED"
        )
    ):
        return closed(
            research=research_stage,
            raw_score=raw_score_stage,
            risk=StageResult(
                phase="RISK_VETO",
                status="FAILED",
                gate_results=(GateResult(gate_id="RISK_RUN", status="FAILED"),),
                reasons=(framework.risk_run_error_code or "RISK_VETO_OUTPUT_INVALID",),
            ),
            validation=_failed_host_validation("RISK_VETO_OUTPUT_INVALID"),
        )
    try:
        research_outcome = freeze_research(
            command,
            envelope,
            legacy=research_mode == "legacy",
            historical=research_mode == "historical",
        )
    except RawScoreCalculationError as error:
        return closed(
            research=research_stage,
            raw_score=StageResult(
                phase="RAW_SCORE",
                status="FAILED",
                gate_results=(GateResult(gate_id="STRUCTURED_Z20", status="FAILED"),),
                reasons=(str(error),),
            ),
            validation=_failed_host_validation("RAW_SCORE_CALCULATION_FAILED"),
        )
    except ValueError:
        return closed(
            research=research_stage,
            raw_score=raw_score_stage,
            risk=StageResult(
                phase="RISK_VETO",
                status="FAILED",
                gate_results=(GateResult(gate_id="RISK_HANDOFF", status="FAILED"),),
                reasons=("RISK_HANDOFF_INVALID",),
            ),
            validation=_failed_host_validation("RESEARCH_HANDOFF_INVALID"),
        )
    result = ExternalResult(
        outcome_code=(
            "RESEARCH_REJECTED" if research_outcome.disposition == "REJECTED" else "RESEARCH_FROZEN"
        ),
        summary=(
            "Synthetic fixed-ten research and independent risk veto were frozen."
            if research_outcome.disposition == "FROZEN"
            else "Synthetic fixed-ten research was rejected by the independent risk veto."
        ),
        key_reasons=research_outcome.reasons,
        research=research_outcome,
    )
    validation_result = host_validation_result(execution_case, result)
    business_result = (
        business_outcome_result(result) if validation_result.status == "SUCCEEDED" else None
    )
    risk_stage = StageResult(
        phase="RISK_VETO",
        status=(
            "REJECTED"
            if research_outcome.risk_veto is not None
            and research_outcome.risk_veto.disposition == "REJECTED"
            else "SUCCEEDED"
        ),
        gate_results=tuple(
            GateResult(gate_id=gate.gate_id, status=gate.status)
            for gate in research_outcome.risk_veto.gates
        )
        if research_outcome.risk_veto is not None
        else (),
        reasons=research_outcome.risk_veto.reasons
        if research_outcome.risk_veto is not None
        else ("RISK_VETO_MISSING",),
    )
    record(research_stage)
    record(raw_score_stage)
    record(risk_stage)
    record(validation_result)
    if business_result is not None:
        record(business_result)
    current_stage_results_before_commit = (
        *framework_stage_results[durable_transition_count:],
        research_stage,
        raw_score_stage,
        risk_stage,
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
    stage_results = (
        *stage_results_before_commit,
        StageResult(
            phase="BUSINESS_COMMIT",
            status="SUCCEEDED",
            gate_results=(GateResult(gate_id="HOST_RESULT_SAVED", status="PASSED"),),
            reasons=(),
        ),
    )
    committed_at = ledger.observed_at()
    attempted_fact = ledger.build_event_fact(
        case=execution_case,
        framework_run_id=framework.run_id,
        result=result,
        stage_results=stage_results,
        committed_at=committed_at,
        generated_at=execution_case.report_generated_at,
    )
    try:
        return ledger.commit_event_fact(connection, attempted_fact)
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
        if risk_run_id is not None:
            _restore_precommit_stage_results(
                ledger,
                connection,
                execution_case,
                risk_stage_results_to_restore,
                framework_run_id=risk_run_id,
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
            business_lifecycle=(
                business_lifecycle_from_stage(business_result)
                or business_lifecycle_from_stage(uncertain_commit)
            ),
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
        if risk_run_id is not None:
            _restore_precommit_stage_results(
                ledger,
                connection,
                execution_case,
                risk_stage_results_to_restore,
                framework_run_id=risk_run_id,
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
            business_lifecycle=business_lifecycle_from_stage(business_result),
            business_commit_status="FAILED",
            stage_results=ledger.get_stage_results(execution_case.business_object_id, connection),
        )


def _publish_committed_fact(
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
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
            business_lifecycle=_business_lifecycle_from_stages(fact.stage_results),
            business_commit_status="COMMITTED",
            stage_results=ledger.get_stage_results(fact.business_object_id, connection),
        )
    assert report is not None
    return _published_execution(report)


def _publish_report_or_record_failure(
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    fact: DecisionEventFact,
    report_version_id: str | None = None,
) -> tuple[FormalReport | None, DecisionEventCommitError | None]:
    """Publish one committed fact or append the exact closed publication boundary."""
    resolved_report_version_id = report_version_id or fact.case.report_version_id_for_event(
        fact.decision_event_id
    )
    ledger.ensure_event_stage_results(connection, fact)
    if fact.case.access_scope is not None and fact.case.access_scope.visibility == "SHADOW":
        _record_fact_stage_result(
            ledger,
            connection,
            fact,
            _publication_failure_stage("SHADOW_ISOLATED"),
        )
        return None, DecisionEventCommitError("shadow results cannot be published")
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
    confirmed_report = ledger.get_formal_report_for_event(fact.decision_event_id, connection)
    assert confirmed_report is not None
    return confirmed_report, None


def _record_publication_failure(
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
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
        _publication_failure_stage(reason),
        allow_repeated_occurrence=True,
    )


def _publication_failure_stage(reason: str) -> StageResult:
    """Keep an acknowledged failure distinct from an unresolved report write."""
    if reason == "PUBLICATION_COMMIT_UNCERTAIN":
        return StageResult(
            phase="PUBLICATION",
            status="UNKNOWN",
            gate_results=(
                GateResult(gate_id="EVENT_COMMITTED", status="PASSED"),
                GateResult(gate_id="FORMAL_REPORT_SAVED", status="UNKNOWN"),
            ),
            reasons=(reason,),
        )
    return StageResult(
        phase="PUBLICATION",
        status="FAILED",
        gate_results=(
            GateResult(gate_id="EVENT_COMMITTED", status="PASSED"),
            GateResult(gate_id="FORMAL_REPORT_SAVED", status="FAILED"),
        ),
        reasons=(reason,),
    )


def _record_fact_stage_result(
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    fact: DecisionEventFact,
    stage_result: StageResult,
    *,
    allow_repeated_occurrence: bool = False,
) -> None:
    """Append event-owned evidence with the current controlled observation time."""
    ledger.record_stage_result(
        connection,
        case=fact.case,
        stage_result=stage_result,
        decision_event_id=fact.decision_event_id,
        framework_run_id=fact.framework_run_id,
        allow_repeated_occurrence=allow_repeated_occurrence,
    )


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
        if case.research is not None and framework.status in {"FAILED", "WAITING"}:
            return _framework_status_stage_result(
                status=framework.status,
                error_code=framework.error_code,
                waiting_reason=framework.waiting_reason,
            )
        return StageResult(
            phase="FRAMEWORK_RUN",
            status="FAILED",
            gate_results=(GateResult(gate_id="ORIGINAL_RUN_IDENTITY", status="FAILED"),),
            reasons=("FRAMEWORK_IDENTITY_MISMATCH",),
        )
    return _framework_status_stage_result(
        status=framework.status,
        error_code=framework.error_code,
        waiting_reason=framework.waiting_reason,
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


def _framework_stage_results_for_auxiliary_run(
    framework: FrameworkRunResult,
) -> tuple[StageResult, ...]:
    """Preserve a secondary framework Run under its own durable identity."""
    if framework.risk_run_id is None or framework.risk_run_status is None:
        return ()
    return _framework_stage_results_for_run(
        status=framework.risk_run_status,
        error_code=framework.risk_run_error_code,
        waiting_reason=framework.risk_waiting_reason,
        transitions=framework.risk_transitions,
        phase="RISK_FRAMEWORK_RUN",
    )


def _framework_stage_results_for_run(
    *,
    status: FrameworkRunStatus,
    error_code: str | None,
    waiting_reason: str | None,
    transitions: tuple[FrameworkRunTransition, ...],
    phase: Literal["FRAMEWORK_RUN", "RISK_FRAMEWORK_RUN"] = "FRAMEWORK_RUN",
) -> tuple[StageResult, ...]:
    final_result = _framework_status_stage_result(
        status=status,
        error_code=error_code,
        waiting_reason=waiting_reason,
        phase=phase,
    )
    transition_results = tuple(
        _framework_transition_stage_result(transition, phase=phase) for transition in transitions
    )
    if transition_results and transition_results[-1] == final_result:
        return transition_results
    return (*transition_results, final_result)


def _record_framework_stage_results(
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    case: FrozenDecisionCase,
    framework_run_id: str,
    stage_results: tuple[StageResult, ...],
    durable_transition_count: int,
) -> None:
    for stage_result in stage_results[durable_transition_count:]:
        ledger.record_stage_result(
            connection,
            case=case,
            stage_result=stage_result,
            framework_run_id=framework_run_id,
            allow_repeated_occurrence=stage_result.status in {"RUNNING", "WAITING"},
        )


def _framework_transition_stage_result(
    transition: FrameworkRunTransition,
    *,
    phase: Literal["FRAMEWORK_RUN", "RISK_FRAMEWORK_RUN"] = "FRAMEWORK_RUN",
) -> StageResult:
    gate_id = {
        "CREATED": "RUN_CREATED",
        "RUNNING": "RUN_ACTIVE",
        "WAITING": "RUN_RECOVERABLE",
        "SUCCEEDED": "RUN_SUCCEEDED",
        "REJECTED": "RUN_REJECTED",
        "FAILED": "RUN_FAILED",
        "CANCELLED": "RUN_CANCELLED",
    }.get(transition.status, "RUN_RECOVERABLE")
    gate_status: Literal["PASSED", "FAILED"] = (
        "FAILED" if transition.status in {"FAILED", "CANCELLED"} else "PASSED"
    )
    return StageResult(
        phase=phase,
        status=transition.status,
        gate_results=(GateResult(gate_id=gate_id, status=gate_status),),
        reasons=(transition.reason,),
    )


def _framework_status_stage_result(
    *,
    status: FrameworkRunStatus,
    error_code: str | None,
    waiting_reason: str | None,
    phase: Literal["FRAMEWORK_RUN", "RISK_FRAMEWORK_RUN"] = "FRAMEWORK_RUN",
) -> StageResult:
    return StageResult(
        phase=phase,
        status=status,
        gate_results=(
            GateResult(gate_id="RUN_TERMINAL", status="PASSED"),
            GateResult(
                gate_id="FRAMEWORK_EXECUTION",
                status="PASSED" if status == "SUCCEEDED" else "FAILED",
            ),
        )
        if status in {"SUCCEEDED", "REJECTED", "FAILED", "CANCELLED"}
        else (GateResult(gate_id="RUN_TERMINAL", status="FAILED"),),
        reasons=(
            ("FRAMEWORK_RUN_SUCCEEDED",)
            if status == "SUCCEEDED"
            else (error_code or waiting_reason or f"FRAMEWORK_{status}",)
        ),
    )


def _framework_run_status_from_stage_history(
    stage_results: tuple[StageResult, ...],
) -> FrameworkRunStatus:
    for stage_result in reversed(stage_results):
        if stage_result.phase == "FRAMEWORK_RUN":
            return framework_run_status_from_stage(stage_result)
    raise RuntimeError("report has no framework run state")


def _restore_precommit_stage_results(
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    case: FrozenDecisionCase,
    stage_results: tuple[StageResult, ...],
    *,
    framework_run_id: str | None = None,
) -> None:
    """Restore durable pre-commit evidence after an event write transaction rolls back."""
    durable_framework_run_id = framework_run_id or case.framework_run_id
    for stage_result in stage_results:
        ledger.record_stage_result(
            connection,
            case=case,
            stage_result=stage_result,
            framework_run_id=durable_framework_run_id,
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
    resolved_commit_reconciliation = False
    for stage_result in reversed(stage_results):
        if stage_result.phase == "BUSINESS_COMMIT" and stage_result.status == "SUCCEEDED":
            resolved_commit_reconciliation = True
        lifecycle = business_lifecycle_from_stage(stage_result)
        if lifecycle is not None and not (
            lifecycle.owner == "COMMIT_RECONCILIATION" and resolved_commit_reconciliation
        ):
            return lifecycle
    return None


def _record_correction_commit_failure(
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    case: FrozenDecisionCase,
    correction_event_id: str,
    framework_run_id: str,
    *,
    uncertain: bool,
) -> None:
    """Keep correction intent and its failed save boundary as append-only evidence."""
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
    )


def _correction_case(
    original_case: FrozenDecisionCase,
    current_case: FrozenDecisionCase,
) -> FrozenDecisionCase:
    """Retain the original Run while recording correction provenance from this host build."""
    return original_case.model_copy(
        update={
            "version_bundle": original_case.version_bundle.model_copy(
                update={
                    "host_application_version": (
                        current_case.version_bundle.host_application_version
                    ),
                    "host_source_sha": current_case.version_bundle.host_source_sha,
                    "report_projection_contract_version": (
                        original_case.version_bundle.report_projection_contract_version
                        if original_case.access_scope is not None
                        else FROZEN_REPORT_PROJECTION_CONTRACT_VERSION
                    ),
                }
            ),
            "recovery_framework_run_id": original_case.framework_run_id,
        }
    )
