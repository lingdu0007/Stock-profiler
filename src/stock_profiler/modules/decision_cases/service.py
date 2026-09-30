"""Host-owned frozen decision journey: Run evidence, validate, commit, then publish."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from threading import Lock
from typing import Literal

from pydantic import ValidationError

from stock_profiler.foundation.decision_versions import CURRENT_M_AGENT_RELEASE
from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    CalibrationRecord,
    CandidateEvidenceClock,
    CandidateReleaseCommand,
    CandidateReleaseOutcome,
    MarketStateQualification,
    MarketStateQualificationStatus,
    candidate_release_availability_failure,
    candidate_release_blocked_by_business_prerequisite,
    finalize_candidate_release_publication,
    freeze_candidate_release,
)
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
    ResultAccessScope,
    StageResult,
    business_lifecycle_from_stage,
    business_outcome_result,
    business_result_status_from_stage,
    framework_run_status_from_stage,
    host_validation_result,
    is_committable_host_outcome,
    research_contract_mode_for_case,
    research_member_run_id,
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
from stock_profiler.modules.qualification.contracts import GovernanceOutcome, QualificationRecord
from stock_profiler.modules.qualification.governance import adjudicate, validate_new_request
from stock_profiler.modules.qualification.service import (
    current_qualification,
    qualification_is_current,
)
from stock_profiler.modules.research.contracts import (
    RAW_SCORE_FEATURE_DATA_TYPES,
    RESEARCH_DEFINITION_ID,
    RESEARCH_LEGACY_ROUTING_POLICY_VERSION,
    RESEARCH_MODEL_ADAPTER_ID,
    RESEARCH_OUTPUT_CONTRACT_ID,
    RESEARCH_ROUTING_POLICY_VERSION,
    RISK_DEFINITION_ID,
    RISK_DEFINITION_VERSION,
    RISK_LEGACY_DEFINITION_VERSION,
    RISK_LEGACY_OUTPUT_CONTRACT_VERSION,
    RISK_MODEL_ADAPTER_ID,
    RISK_OUTPUT_CONTRACT_ID,
    RISK_OUTPUT_CONTRACT_VERSION,
    RawScore,
    RawScoreCalculationError,
    RawScoreTrainingRecord,
    ResearchCommand,
    ResearchDraft,
    ResearchDraftMember,
    ResearchFrameworkOutput,
    ResearchMemberInput,
    ResearchOutcome,
    ResearchRiskPlan,
    ResearchToolEvidence,
    RiskVetoDraft,
    decode_historical_research_draft,
    decode_historical_research_draft_member,
    decode_historical_research_framework_output,
    decode_legacy_research_draft,
    decode_legacy_research_framework_output,
    raw_score_maturity_at,
    research_contract_mode_for_versions,
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


class CandidateQualificationVersionMismatch(ValueError):
    """Persist an availability failure when the saved qualification version is stale."""

    def __init__(self) -> None:
        super().__init__("CANDIDATE_QUALIFICATION_VERSION_MISMATCH")


class CandidateQualificationHistoryAmbiguous(ValueError):
    """Reject conflicting terminal revisions instead of selecting by timestamp order."""

    def __init__(self) -> None:
        super().__init__("CANDIDATE_QUALIFICATION_HISTORY_AMBIGUOUS")


class CandidateCalibrationProvenanceInvalid(ValueError):
    """Persist a calibration availability failure when frozen source lineage is invalid."""

    def __init__(self) -> None:
        super().__init__("CANDIDATE_CALIBRATION_LINEAGE_INVALID")


class CandidateCalibrationVersionMismatch(ValueError):
    """Fail visibly when calibration labels use a different raw-score model version."""

    def __init__(self) -> None:
        super().__init__("CANDIDATE_CALIBRATION_MODEL_VERSION_MISMATCH")


class CandidateResearchVersionMismatch(ValueError):
    """Persist an availability failure when an upstream handoff version is incompatible."""

    def __init__(self) -> None:
        super().__init__("CANDIDATE_RESEARCH_VERSION_MISMATCH")


@dataclass(frozen=True)
class _FrozenCandidatePrediction:
    raw_success_score: Decimal
    calibrated_probability: Decimal
    raw_score_frozen_at: datetime
    raw_score_training_watermark_at: datetime
    market_calendar_version: str
    entry_window_ends_at: datetime
    entry_sessions: tuple[tuple[datetime, datetime], ...]
    matures_by: datetime


class CandidateResearchHandoffUnavailable(ValueError):
    """Preserve an unavailable upstream research result in its candidate batch."""

    def __init__(self, disposition: str):
        self.disposition = disposition
        self.reason = (
            "RESEARCH_PREREQUISITE_BLOCKED"
            if disposition == "BLOCKED"
            else "CANDIDATE_RESEARCH_HANDOFF_UNAVAILABLE"
        )
        super().__init__(self.reason)


_FRAMEWORK_EXECUTION_LOCKS: dict[str, Lock] = {}
_FRAMEWORK_EXECUTION_LOCKS_GUARD = Lock()


ResearchMemberExecutor = Callable[[int, ResearchMemberInput, str], Awaitable[FrameworkRunResult]]


async def execute_research_member_runs(
    case: FrozenDecisionCase,
    *,
    execute_member: ResearchMemberExecutor,
    record_transition: Callable[[FrameworkRunTransition], Awaitable[None]] | None = None,
    historical: bool = False,
) -> FrameworkRunResult:
    """Schedule and aggregate member Runs as a host-owned cohort decision."""
    command = case.research
    assert command is not None
    member_results: list[ResearchMemberRunResult] = []
    member_drafts: list[ResearchDraftMember] = []
    member_draft_indexes: list[int] = []
    member_transitions: list[FrameworkRunTransition] = []
    primary_transitions: tuple[FrameworkRunTransition, ...] = ()
    primary_transitions_durably_recorded = False
    tool_evidence: list[ResearchToolEvidence] = []
    first_waiting_reason: str | None = None
    all_runs_existed = True

    for index, member in enumerate(command.members):
        run_id = research_member_run_id(
            case.framework_run_id,
            index,
            member.security_id,
            member.research_id,
        )
        try:
            result = await execute_member(index, member, run_id)
        except (MappedDurableRunMissingError, ValueError) as error:
            if not member_results:
                raise
            error_code = (
                "RESEARCH_RUN_MISSING"
                if isinstance(error, MappedDurableRunMissingError)
                else "RESEARCH_RUN_RECOVERY_FAILED"
            )
            transition = FrameworkRunTransition(
                status="FAILED",
                reason=error_code,
                run_id=run_id,
            )
            if record_transition is not None:
                await record_transition(transition)
            result = FrameworkRunResult(
                run_id=run_id,
                status="FAILED",
                output=None,
                error_code=error_code,
                transitions=(transition,),
                transitions_durably_recorded=record_transition is not None,
            )

        all_runs_existed = all_runs_existed and result.run_existed_before
        member_transitions.extend(result.transitions)
        if result.status == "WAITING" and first_waiting_reason is None:
            first_waiting_reason = result.waiting_reason
        if index == 0:
            primary_transitions = result.transitions
            primary_transitions_durably_recorded = result.transitions_durably_recorded
        member_status = result.status
        member_error_code = result.error_code
        if result.status == "SUCCEEDED" and result.error_code is not None:
            member_status = "FAILED"
        elif result.status == "SUCCEEDED" and result.output is not None:
            try:
                draft_member = (
                    decode_historical_research_draft_member(json.loads(result.output))
                    if historical
                    else ResearchDraftMember.model_validate_json(result.output)
                )
            except ValueError:
                member_status = "FAILED"
                member_error_code = "RESEARCH_OUTPUT_INVALID"
            else:
                if (
                    draft_member.security_id != member.security_id
                    or draft_member.research_id != member.research_id
                ):
                    member_status = "FAILED"
                    member_error_code = "RESEARCH_OUTPUT_INVALID"
                else:
                    member_drafts.append(draft_member)
                    member_draft_indexes.append(index)
                    for evidence in result.research_tool_evidence:
                        if evidence.evidence_id not in {item.evidence_id for item in tool_evidence}:
                            tool_evidence.append(evidence)
        elif result.status == "SUCCEEDED":
            member_status = "FAILED"
            member_error_code = "RESEARCH_OUTPUT_MISSING"
        member_results.append(
            ResearchMemberRunResult(
                security_id=member.security_id,
                research_id=member.research_id,
                run_id=run_id,
                status=member_status,
                error_code=member_error_code,
            )
        )

    member_run_results = tuple(member_results)
    failed_results = tuple(
        result for result in member_run_results if result.status in {"FAILED", "REJECTED"}
    )
    waiting_results = tuple(result for result in member_run_results if result.status == "WAITING")
    aggregate_status: FrameworkRunStatus = (
        "FAILED" if failed_results else "WAITING" if waiting_results else "SUCCEEDED"
    )
    error_codes = tuple(result.error_code for result in member_run_results if result.error_code)
    if aggregate_status != "SUCCEEDED":
        aggregate_run_id = (
            failed_results[0].run_id
            if failed_results
            else waiting_results[0].run_id
            if waiting_results
            else case.framework_run_id
        )
        return FrameworkRunResult(
            run_id=aggregate_run_id,
            status=aggregate_status,
            output=None,
            run_existed_before=all_runs_existed,
            waiting_reason=(
                first_waiting_reason
                if waiting_results and first_waiting_reason is not None
                else "RESEARCH_MEMBER_RUN_WAITING"
                if waiting_results
                else None
            ),
            error_code=(
                error_codes[0]
                if error_codes
                else "RESEARCH_MEMBER_RUN_FAILED"
                if aggregate_status == "FAILED"
                else None
            ),
            research_member_runs=member_run_results,
            transitions=(
                primary_transitions
                if aggregate_run_id == case.framework_run_id
                else tuple(member_transitions)
            ),
            transitions_durably_recorded=(
                primary_transitions_durably_recorded
                if aggregate_run_id == case.framework_run_id
                else False
            ),
            research_tool_evidence=tuple(tool_evidence),
        )

    try:
        draft = ResearchDraft(contract_version="1.0.0", members=tuple(member_drafts))
    except ValueError:
        invalid_member_indexes = {
            index
            for index, draft_member in zip(member_draft_indexes, member_drafts, strict=True)
            if (
                draft_member.security_id != command.members[index].security_id
                or draft_member.research_id != command.members[index].research_id
            )
        }
        failed_members = tuple(
            replace(result, status="FAILED", error_code="RESEARCH_OUTPUT_INVALID")
            if index in invalid_member_indexes
            else result
            for index, result in enumerate(member_run_results)
        )
        return FrameworkRunResult(
            run_id=case.framework_run_id,
            status="FAILED",
            output=None,
            run_existed_before=all_runs_existed,
            error_code="RESEARCH_OUTPUT_INVALID",
            research_member_runs=failed_members,
            transitions=primary_transitions,
            transitions_durably_recorded=record_transition is not None,
            research_tool_evidence=tuple(tool_evidence),
        )
    return FrameworkRunResult(
        run_id=case.framework_run_id,
        status="SUCCEEDED",
        output=json.dumps(
            research_draft_payload(draft, historical=historical),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        run_existed_before=all_runs_existed,
        research_member_runs=member_run_results,
        transitions=primary_transitions,
        transitions_durably_recorded=record_transition is not None,
        research_tool_evidence=tuple(tool_evidence),
    )


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


def _validate_candidate_release_source(
    case: FrozenDecisionCase,
    source_event: DecisionEventFact | None,
) -> str:
    """Require every candidate input to match one committed research and risk result."""
    command = case.candidate_release
    assert command is not None
    if source_event is None:
        raise CandidateResearchHandoffUnavailable("MISSING")
    if (
        source_event.decision_event_id != command.research_event_id
        or source_event.business_object_id != command.research_object_id
        or source_event.corrects_event_id is not None
        or source_event.validation_status != "PASSED"
        or source_event.case.access_scope is None
        or case.access_scope is None
        or not source_event.case.access_scope.same_scope_as(case.access_scope)
        or datetime.fromisoformat(source_event.case.knowledge_cutoff)
        != datetime.fromisoformat(case.knowledge_cutoff)
        or source_event.result.research is None
    ):
        raise ValueError("CANDIDATE_RESEARCH_EVENT_INVALID")
    research = source_event.result.research
    if research.disposition in {"DATA_FAILED", "SYSTEM_FAILED", "BLOCKED"}:
        raise CandidateResearchHandoffUnavailable(research.disposition)
    risk = research.risk_veto
    raw_scores = research.raw_scores
    if research.disposition not in {"FROZEN", "REJECTED"}:
        raise ValueError("CANDIDATE_RESEARCH_HANDOFF_INCOMPLETE")
    if risk is None or raw_scores is None:
        raise CandidateResearchHandoffUnavailable("INCOMPLETE")
    if not _candidate_research_versions_are_compatible(source_event.case, research):
        raise CandidateResearchVersionMismatch()
    model_versions = {score.model_version for score in raw_scores}
    if len(model_versions) != 1:
        raise CandidateCalibrationVersionMismatch()
    members = {member.security_id: member for member in research.members}
    source_member_inputs = (
        {member.security_id: member for member in source_event.case.research.members}
        if source_event.case.research is not None
        else {}
    )
    scores = {score.security_id: score for score in raw_scores}
    vetoes = {veto.security_id: veto for veto in risk.member_vetoes}
    source_knowledge_cutoff = datetime.fromisoformat(
        source_event.case.knowledge_cutoff.replace("Z", "+00:00")
    )
    if (
        len(members) != len(research.members)
        or len(scores) != len(raw_scores)
        or set(members) != set(scores)
        or set(members) != set(vetoes)
        or set(members) != {member.security_id for member in command.candidates}
        or len(command.candidates) != 10
    ):
        raise ValueError("CANDIDATE_RESEARCH_COHORT_MISMATCH")
    for candidate in command.candidates:
        member = members[candidate.security_id]
        score = scores[candidate.security_id]
        veto = vetoes[candidate.security_id]
        if candidate.research_id != member.research_id:
            raise ValueError("CANDIDATE_RESEARCH_ID_MISMATCH")
        if candidate.raw_success_score != score.z20:
            raise ValueError("CANDIDATE_RAW_SCORE_MISMATCH")
        if (
            candidate.risk_status != veto.disposition
            or tuple((gate.gate_id, gate.status) for gate in candidate.risk_gates)
            != tuple((gate.gate_id, gate.status) for gate in veto.gates)
            or candidate.risk_reasons != veto.reasons
        ):
            raise ValueError("CANDIDATE_RISK_VETO_MISMATCH")
        if candidate.thesis != member.thesis or candidate.principal_risks != (member.bear_case,):
            raise ValueError("CANDIDATE_THESIS_OR_RISK_MISMATCH")
        expected_freshness = (
            "FRESH_AT_KNOWLEDGE_CUTOFF"
            if member.knowledge_cutoff == source_knowledge_cutoff
            else "STALE_AT_KNOWLEDGE_CUTOFF"
        )
        if candidate.evidence_freshness != expected_freshness:
            raise ValueError("CANDIDATE_EVIDENCE_FRESHNESS_MISMATCH")
        source_member_input = source_member_inputs.get(candidate.security_id)
        expected_evidence_clocks = (
            tuple(
                CandidateEvidenceClock(
                    evidence_id=evidence.evidence_id,
                    effective_at=evidence.effective_at,
                    source_published_at=evidence.source_published_at,
                    acquired_at=evidence.acquired_at,
                    validated_at=evidence.validated_at,
                    knowledge_cutoff=evidence.knowledge_cutoff,
                )
                for evidence in source_member_input.evidence
            )
            if source_member_input is not None
            else ()
        )
        if candidate.evidence_clocks != expected_evidence_clocks:
            raise ValueError("CANDIDATE_EVIDENCE_CLOCKS_MISMATCH")
    return next(iter(model_versions))


def _candidate_research_versions_are_compatible(
    source_case: FrozenDecisionCase,
    research: ResearchOutcome,
) -> bool:
    """Accept only supported, internally consistent research and risk handoff versions."""
    handoff = research.handoff
    source_bundle = source_case.version_bundle
    try:
        mode = research_contract_mode_for_versions(
            handoff.research_definition_version,
            handoff.research_output_contract_version,
        )
    except ValueError:
        return False
    legacy = mode == "legacy"
    historical = mode == "historical"
    expected_risk_definition_version = (
        RISK_LEGACY_DEFINITION_VERSION if legacy or historical else RISK_DEFINITION_VERSION
    )
    expected_risk_output_contract_version = (
        RISK_LEGACY_OUTPUT_CONTRACT_VERSION
        if legacy or historical
        else RISK_OUTPUT_CONTRACT_VERSION
    )
    expected_routing_policy_version = (
        RESEARCH_LEGACY_ROUTING_POLICY_VERSION if legacy else RESEARCH_ROUTING_POLICY_VERSION
    )
    return (
        handoff.research_definition_id == RESEARCH_DEFINITION_ID
        and source_bundle.agent_definition_id == handoff.research_definition_id
        and source_bundle.agent_definition_version == handoff.research_definition_version
        and handoff.research_model_adapter_id == RESEARCH_MODEL_ADAPTER_ID
        and source_bundle.model_adapter_id == handoff.research_model_adapter_id
        and handoff.research_routing_policy_version == expected_routing_policy_version
        and source_bundle.routing_policy_version == handoff.research_routing_policy_version
        and handoff.research_output_contract_id == RESEARCH_OUTPUT_CONTRACT_ID
        and source_bundle.output_contract_version == handoff.research_output_contract_version
        and handoff.risk_definition_id == RISK_DEFINITION_ID
        and handoff.risk_definition_version == expected_risk_definition_version
        and handoff.risk_model_adapter_id == RISK_MODEL_ADAPTER_ID
        and handoff.risk_output_contract_id == RISK_OUTPUT_CONTRACT_ID
        and handoff.risk_output_contract_version == expected_risk_output_contract_version
        and research.risk_veto is not None
        and research.risk_veto.definition_id == handoff.risk_definition_id
        and research.risk_veto.definition_version == handoff.risk_definition_version
    )


def _validate_candidate_calibration_sources(
    command: CandidateReleaseCommand,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    access_scope: ResultAccessScope,
    candidate_model_version: str,
) -> tuple[bool, tuple[CalibrationRecord, ...]]:
    """Bind calibration rows to the complete frozen out-of-sample research snapshot."""
    if any(
        record.raw_score_model_version != candidate_model_version
        for record in command.training_records
    ):
        raise CandidateCalibrationVersionMismatch()

    candidate_cutoff = datetime.fromisoformat(command.knowledge_cutoff.isoformat())
    candidate_release_events = ledger.candidate_release_history(connection, access_scope)
    prior_calibration_snapshots = tuple(
        event
        for event in candidate_release_events
        if _prior_calibration_snapshot_matches(event, command)
    )
    unavailable_candidate_probability_identities = _unavailable_candidate_probability_identities(
        candidate_release_events,
        candidate_cutoff,
    )
    if prior_calibration_snapshots and len(command.training_window_months) != 60:
        raise CandidateCalibrationProvenanceInvalid()
    historical_events = tuple(
        sorted(
            (
                event
                for event in ledger.research_event_history(connection, access_scope)
                if event.corrects_event_id is None
                and event.validation_status == "PASSED"
                and datetime.fromisoformat(event.case.knowledge_cutoff) <= candidate_cutoff
                and event.case.research is not None
                and event.result.research is not None
            ),
            key=lambda event: event.decision_event_id,
        )
    )
    historical_events_by_id = {event.decision_event_id: event for event in historical_events}
    mature_source_rows: dict[tuple[str, str, str], RawScoreTrainingRecord] = {}
    mature_source_event_ids: dict[tuple[str, str, str], str] = {}
    unavailable_probability_identities: set[tuple[str, str, str]] = set()
    cutoff_mature_source_rows: dict[tuple[str, str, str], RawScoreTrainingRecord] = {}
    cutoff_mature_source_event_ids: dict[tuple[str, str, str], str] = {}
    frozen_training_rows: dict[tuple[str, str, str], CalibrationRecord] = {}
    cutoff_frozen_training_rows: dict[tuple[str, str, str], CalibrationRecord] = {}
    frozen_candidate_predictions: dict[tuple[str, str, str], _FrozenCandidatePrediction] = {}
    cutoff_incomplete_source_months: set[str] = set()
    for event in prior_calibration_snapshots:
        prior_command = event.case.candidate_release
        prior_outcome = event.result.candidate_release
        if prior_command is None or prior_outcome is None:
            continue
        if prior_outcome.calibration is not None:
            research_event = historical_events_by_id.get(prior_command.research_event_id)
            predictions = _frozen_candidate_prediction_rows_from_events(event, research_event)
            for identity, prediction in predictions.items():
                previous_prediction = frozen_candidate_predictions.setdefault(identity, prediction)
                if previous_prediction != prediction:
                    raise CandidateCalibrationProvenanceInvalid()
        for record in prior_command.training_records:
            if (
                record.label_available_at <= candidate_cutoff
                and record.unified_maturity_at <= candidate_cutoff
            ):
                _retain_frozen_calibration_record(cutoff_frozen_training_rows, record)
            if (
                record.label_available_at > command.label_watermark_at
                or record.unified_maturity_at > command.label_watermark_at
            ):
                continue
            _retain_frozen_calibration_record(frozen_training_rows, record)
    for event in historical_events:
        source_case_research = event.case.research
        if source_case_research is None:
            continue
        model = source_case_research.raw_score_model
        if model.calibration_evidence_version != "frozen-oos-calibration-v2":
            continue
        for source_record in model.training_records:
            if source_record.unified_maturity_at is None:
                raise CandidateCalibrationProvenanceInvalid()
            identity = (
                source_record.month,
                source_record.security_id,
                source_record.research_id,
            )
            if source_record.historical_calibrated_probability is None:
                unavailable_probability_identities.add(identity)
            if (
                source_record.label_available_at <= candidate_cutoff
                and source_record.unified_maturity_at <= candidate_cutoff
            ):
                if source_record.historical_calibrated_probability is not None:
                    _retain_immutable_mature_source_row(
                        cutoff_mature_source_rows, identity, source_record
                    )
                    cutoff_mature_source_event_ids.setdefault(identity, event.decision_event_id)
            else:
                cutoff_incomplete_source_months.add(source_record.month)
            if (
                source_record.historical_calibrated_probability is not None
                and source_record.label_available_at <= command.label_watermark_at
                and source_record.unified_maturity_at <= command.label_watermark_at
            ):
                _retain_immutable_mature_source_row(mature_source_rows, identity, source_record)
                mature_source_event_ids.setdefault(identity, event.decision_event_id)
    unavailable_probability_identities.difference_update(frozen_training_rows)
    unavailable_probability_identities.difference_update(cutoff_frozen_training_rows)
    unavailable_probability_identities.update(unavailable_candidate_probability_identities)
    for identity in unavailable_probability_identities:
        mature_source_rows.pop(identity, None)
        mature_source_event_ids.pop(identity, None)
        cutoff_mature_source_rows.pop(identity, None)
        cutoff_mature_source_event_ids.pop(identity, None)
    cutoff_matured_candidate_prediction_ids = _matured_candidate_prediction_ids(
        frozen_candidate_predictions,
        cutoff_mature_source_rows,
        candidate_cutoff,
    )
    globally_mature_months = _fully_matured_calibration_months(
        set(cutoff_mature_source_rows),
        set(cutoff_frozen_training_rows),
        cutoff_matured_candidate_prediction_ids,
        set(frozen_candidate_predictions),
        cutoff_incomplete_source_months,
    )
    _validate_calibration_training_window(
        command.training_window_months,
        globally_mature_months,
        initial_calibration=not prior_calibration_snapshots,
    )
    recent_diagnostic_months = set(_recent_calibration_month_window(globally_mature_months))
    recent_diagnostic_records_by_identity = _recent_calibration_diagnostic_records(
        cutoff_mature_source_rows,
        cutoff_mature_source_event_ids,
        cutoff_frozen_training_rows,
        recent_diagnostic_months,
        unavailable_probability_identities,
    )
    records_by_event = _calibration_records_by_source_event(command.training_records)
    for records in records_by_event.values():
        for record in records:
            identity = (record.month, record.security_id, record.research_id)
            if identity in unavailable_probability_identities:
                raise CandidateCalibrationProvenanceInvalid()

    for event_id, records in records_by_event.items():
        source_event = ledger.get_decision_event(event_id, connection)
        if (
            source_event is None
            or source_event.decision_event_id != event_id
            or source_event.corrects_event_id is not None
            or source_event.validation_status != "PASSED"
            or datetime.fromisoformat(source_event.case.knowledge_cutoff) > candidate_cutoff
            or source_event.case.access_scope is None
            or not source_event.case.access_scope.same_scope_as(access_scope)
            or source_event.result.research is None
            or source_event.case.research is None
        ):
            raise CandidateCalibrationProvenanceInvalid()
        research = source_event.result.research
        raw_scores = research.raw_scores
        if (
            research.disposition not in {"FROZEN", "REJECTED"}
            or raw_scores is None
            or not raw_scores
        ):
            raise CandidateCalibrationProvenanceInvalid()
        snapshot_records = source_event.case.research.raw_score_model.training_records
        records_by_identity = {
            (record.month, record.security_id, record.research_id): record for record in records
        }
        if len(records_by_identity) != len(records):
            raise CandidateCalibrationProvenanceInvalid()
        submitted_months = {record.month for record in records}
        snapshot_by_identity = _calibration_source_rows_by_identity(
            snapshot_records, submitted_months
        )
        frozen_by_identity = {
            identity: record
            for identity, record in frozen_training_rows.items()
            if record.source_research_event_id == event_id
        }
        expected_source_rows = snapshot_by_identity
        expected_identities = set(expected_source_rows) | {
            identity for identity in frozen_by_identity if identity[0] in submitted_months
        }
        if not set(records_by_identity).issubset(expected_identities):
            raise CandidateCalibrationProvenanceInvalid()
        for identity, record in records_by_identity.items():
            frozen_record = frozen_by_identity.get(identity)
            if frozen_record is not None:
                if not _same_frozen_calibration_record(record, frozen_record):
                    raise CandidateCalibrationProvenanceInvalid()
                continue
            source_record = expected_source_rows[identity]
            if (
                source_record.raw_score_frozen_at != record.raw_score_frozen_at
                or source_record.raw_score_training_watermark_at
                != record.raw_score_training_watermark_at
                or source_record.raw_success_score != record.raw_success_score
                or source_record.historical_calibrated_probability
                != record.out_of_sample_probability
                or source_record.source_model_version != record.raw_score_model_version
                or source_record.market_calendar_version != record.market_calendar_version
                or source_record.terminal_label != record.terminal_success
                or source_record.evaluation_entry_at != record.entry_at
                or source_record.entry_window_ends_at != record.entry_window_ends_at
                or source_record.unified_maturity_at != record.unified_maturity_at
                or source_record.label_available_at != record.label_available_at
                or source_record.raw_score_frozen_at is None
                or source_record.raw_score_training_watermark_at is None
                or source_record.raw_success_score is None
                or source_record.entry_window_ends_at is None
                or source_record.unified_maturity_at is None
                or source_record.raw_score_frozen_at > candidate_cutoff
                or source_record.raw_score_training_watermark_at > source_record.raw_score_frozen_at
            ):
                raise CandidateCalibrationProvenanceInvalid()
            if datetime.fromisoformat(source_event.committed_at) < record.label_available_at:
                raise CandidateCalibrationProvenanceInvalid()
        if not all(
            record.unified_maturity_at <= command.label_watermark_at
            and record.label_available_at <= command.label_watermark_at
            for record in records
        ):
            raise CandidateCalibrationProvenanceInvalid()
    if len(command.training_window_months) >= 60:
        _validate_cutoff_complete_calibration_population(
            command.training_window_months,
            cutoff_mature_source_rows,
            cutoff_frozen_training_rows,
            cutoff_matured_candidate_prediction_ids,
            {
                (record.month, record.security_id, record.research_id)
                for record in command.training_records
            },
        )
        for record in command.training_records:
            latest_source_record = mature_source_rows.get(
                (record.month, record.security_id, record.research_id)
            )
            frozen_record = frozen_training_rows.get(
                (record.month, record.security_id, record.research_id)
            )
            if latest_source_record is None and frozen_record is None:
                raise CandidateCalibrationProvenanceInvalid()
            if frozen_record is not None and not _same_frozen_calibration_record(
                record, frozen_record
            ):
                raise CandidateCalibrationProvenanceInvalid()
            if latest_source_record is not None and (
                latest_source_record.raw_success_score != record.raw_success_score
                or latest_source_record.historical_calibrated_probability
                != record.out_of_sample_probability
                or latest_source_record.terminal_label != record.terminal_success
                or latest_source_record.raw_score_frozen_at != record.raw_score_frozen_at
                or latest_source_record.unified_maturity_at != record.unified_maturity_at
                or latest_source_record.label_available_at != record.label_available_at
            ):
                raise CandidateCalibrationProvenanceInvalid()
    return (
        not prior_calibration_snapshots,
        tuple(
            recent_diagnostic_records_by_identity[identity]
            for identity in sorted(recent_diagnostic_records_by_identity)
        ),
    )


def _prior_calibration_snapshot_matches(
    event: DecisionEventFact,
    command: CandidateReleaseCommand,
) -> bool:
    """Include prior frozen fits based on their domain cutoff, not caller clocks."""
    release = event.result.candidate_release
    return bool(
        release is not None
        and release.calibration is not None
        and release.calibration.calibrator_version == command.calibrator_version
        and datetime.fromisoformat(event.case.knowledge_cutoff)
        <= datetime.fromisoformat(command.knowledge_cutoff.isoformat())
    )


def _unavailable_candidate_probability_identities(
    candidate_release_events: tuple[DecisionEventFact, ...],
    knowledge_cutoff: datetime,
) -> set[tuple[str, str, str]]:
    """Keep failed no-probability predictions out of all later calibration cohorts."""
    unavailable: set[tuple[str, str, str]] = set()
    for event in candidate_release_events:
        if datetime.fromisoformat(event.case.knowledge_cutoff) > knowledge_cutoff:
            continue
        command = event.case.candidate_release
        outcome = event.result.candidate_release
        if command is None or outcome is None:
            continue
        month = _candidate_prediction_month(command.knowledge_cutoff)
        unavailable.update(
            (month, member.security_id, member.research_id)
            for member in outcome.members
            if member.calibrated_probability is None
        )
    return unavailable


def _validate_calibration_training_window(
    training_window_months: tuple[str, ...],
    authoritative_mature_months: tuple[str, ...],
    *,
    initial_calibration: bool = False,
) -> None:
    """Bind the initial prefix or later rolling window to cutoff-mature months."""
    if len(training_window_months) < 60:
        return
    if len(authoritative_mature_months) < len(training_window_months):
        raise CandidateCalibrationProvenanceInvalid()
    expected_window = (
        authoritative_mature_months[: len(training_window_months)]
        if initial_calibration
        else authoritative_mature_months[-len(training_window_months) :]
    )
    if training_window_months != expected_window:
        raise CandidateCalibrationProvenanceInvalid()


def _fully_matured_calibration_months(
    mature_source_identities: set[tuple[str, str, str]],
    mature_frozen_calibration_identities: set[tuple[str, str, str]],
    mature_candidate_prediction_ids: set[tuple[str, str, str]],
    all_candidate_prediction_ids: set[tuple[str, str, str]],
    incomplete_source_months: set[str],
) -> tuple[str, ...]:
    """Count a month only after every frozen prediction and source label has matured."""
    mature_months = {
        identity[0]
        for identity in (
            mature_source_identities
            | mature_frozen_calibration_identities
            | mature_candidate_prediction_ids
        )
    }
    pending_prediction_months = {
        identity[0] for identity in all_candidate_prediction_ids - mature_candidate_prediction_ids
    }
    return tuple(sorted(mature_months - incomplete_source_months - pending_prediction_months))


def _calibration_source_rows_by_identity(
    records: Iterable[RawScoreTrainingRecord],
    months: set[str],
    excluded_identities: set[tuple[str, str, str]] | None = None,
) -> dict[tuple[str, str, str], RawScoreTrainingRecord]:
    """Include only matured-probability evidence eligible for calibration."""
    excluded = excluded_identities or set()
    return {
        (record.month, record.security_id, record.research_id): record
        for record in records
        if record.month in months
        and record.historical_calibrated_probability is not None
        and (record.month, record.security_id, record.research_id) not in excluded
    }


def _calibration_records_by_source_event(
    records: Iterable[CalibrationRecord],
) -> dict[str, list[CalibrationRecord]]:
    """Partition rows by provenance while allowing a mature month to span events."""
    grouped: dict[str, list[CalibrationRecord]] = {}
    for record in records:
        grouped.setdefault(record.source_research_event_id, []).append(record)
    return grouped


def _recent_calibration_diagnostic_records(
    mature_source_rows: dict[tuple[str, str, str], RawScoreTrainingRecord],
    mature_source_event_ids: dict[tuple[str, str, str], str],
    frozen_training_rows: dict[tuple[str, str, str], CalibrationRecord],
    recent_months: set[str],
    unavailable_probability_identities: set[tuple[str, str, str]],
) -> dict[tuple[str, str, str], CalibrationRecord]:
    """Preserve committed attribution before adding newly observed source diagnostics."""
    records = {
        identity: record
        for identity, record in frozen_training_rows.items()
        if identity[0] in recent_months
    }
    for identity, source in mature_source_rows.items():
        if (
            identity[0] in recent_months
            and identity not in unavailable_probability_identities
            and identity not in records
        ):
            records[identity] = _calibration_record_from_raw_score(
                source, mature_source_event_ids[identity]
            )
    return records


def _recent_calibration_month_window(
    authoritative_mature_months: tuple[str, ...],
) -> tuple[str, ...]:
    """Select diagnostics from the latest mature months known at the candidate cutoff."""
    return tuple(sorted(authoritative_mature_months)[-24:])


def _calibration_record_from_raw_score(
    source: RawScoreTrainingRecord,
    source_research_event_id: str,
) -> CalibrationRecord:
    """Project one immutable raw-score outcome into the calibration evidence shape."""
    if (
        source.raw_score_frozen_at is None
        or source.raw_score_training_watermark_at is None
        or source.raw_success_score is None
        or source.historical_calibrated_probability is None
        or source.entry_window_ends_at is None
        or source.unified_maturity_at is None
        or source.market_calendar_version is None
    ):
        raise CandidateCalibrationProvenanceInvalid()
    return CalibrationRecord(
        record_id=(f"recent-diagnostic-{source.month}-{source.security_id}-{source.research_id}"),
        month=source.month,
        source_research_event_id=source_research_event_id,
        security_id=source.security_id,
        research_id=source.research_id,
        raw_score_model_version=source.source_model_version,
        raw_score_frozen_at=source.raw_score_frozen_at,
        raw_score_training_watermark_at=source.raw_score_training_watermark_at,
        raw_success_score=source.raw_success_score,
        out_of_sample_probability=source.historical_calibrated_probability,
        terminal_success=source.terminal_label,
        entry_at=source.evaluation_entry_at,
        entry_window_ends_at=source.entry_window_ends_at,
        unified_maturity_at=source.unified_maturity_at,
        label_available_at=source.label_available_at,
        market_calendar_version=source.market_calendar_version,
    )


def _same_frozen_calibration_record(left: CalibrationRecord, right: CalibrationRecord) -> bool:
    """Compare immutable sample content while allowing record IDs to be regenerated."""
    return left.model_copy(update={"record_id": right.record_id}) == right


def _frozen_candidate_prediction_rows_from_events(
    candidate_event: DecisionEventFact,
    research_event: DecisionEventFact | None,
) -> dict[tuple[str, str, str], _FrozenCandidatePrediction]:
    """Resolve a prior release's separate source event before replaying its predictions."""
    command = candidate_event.case.candidate_release
    outcome = candidate_event.result.candidate_release
    if command is None or outcome is None or research_event is None:
        raise CandidateCalibrationProvenanceInvalid()
    if (
        research_event.decision_event_id != command.research_event_id
        or research_event.business_object_id != command.research_object_id
        or datetime.fromisoformat(research_event.case.knowledge_cutoff)
        != datetime.fromisoformat(candidate_event.case.knowledge_cutoff)
    ):
        raise CandidateCalibrationProvenanceInvalid()
    return _frozen_candidate_prediction_rows_from_source(
        command,
        outcome,
        research_event.case.research,
        research_event.result.research,
    )


def _frozen_candidate_prediction_rows_from_source(
    command: CandidateReleaseCommand,
    outcome: CandidateReleaseOutcome,
    research_command: ResearchCommand | None,
    research_outcome: ResearchOutcome | None,
) -> dict[tuple[str, str, str], _FrozenCandidatePrediction]:
    """Freeze probabilities against the separate research event's score clocks."""
    if (
        research_command is None
        or research_outcome is None
        or research_outcome.raw_scores is None
        or not research_outcome.raw_scores
        or research_command.raw_score_model.label_watermark_at is None
    ):
        raise CandidateCalibrationProvenanceInvalid()
    raw_score_training_watermark_at = research_command.raw_score_model.label_watermark_at
    if (
        any(
            raw_score.label_watermark_at != raw_score_training_watermark_at
            for raw_score in research_outcome.raw_scores
        )
        or raw_score_training_watermark_at > command.knowledge_cutoff
    ):
        raise CandidateCalibrationProvenanceInvalid()
    return _frozen_candidate_prediction_rows(
        command,
        outcome,
        raw_score_frozen_at=command.knowledge_cutoff,
        raw_score_training_watermark_at=raw_score_training_watermark_at,
    )


def _frozen_candidate_prediction_rows(
    command: CandidateReleaseCommand,
    outcome: CandidateReleaseOutcome,
    *,
    raw_score_frozen_at: datetime,
    raw_score_training_watermark_at: datetime,
) -> dict[tuple[str, str, str], _FrozenCandidatePrediction]:
    """Retain every frozen member probability, regardless of later gate disposition."""
    if outcome.calibration is None:
        return {}
    if (
        raw_score_frozen_at != command.knowledge_cutoff
        or raw_score_training_watermark_at > raw_score_frozen_at
    ):
        raise CandidateCalibrationProvenanceInvalid()
    if not outcome.valid_market_dates:
        raise CandidateCalibrationProvenanceInvalid()
    sessions_by_date = {session.market_date: session for session in command.market_sessions}
    window_sessions = tuple(
        sessions_by_date.get(market_date) for market_date in outcome.valid_market_dates
    )
    if any(session is None for session in window_sessions):
        raise CandidateCalibrationProvenanceInvalid()
    final_window_session = window_sessions[-1]
    assert final_window_session is not None
    prediction_month = _candidate_prediction_month(command.knowledge_cutoff)
    matures_by = raw_score_maturity_at(
        final_window_session.closes_at, command.market_calendar_version
    )
    predictions: dict[tuple[str, str, str], _FrozenCandidatePrediction] = {}
    for member in outcome.members:
        if member.calibrated_probability is None:
            continue
        identity = (prediction_month, member.security_id, member.research_id)
        if identity in predictions:
            raise CandidateCalibrationProvenanceInvalid()
        predictions[identity] = _FrozenCandidatePrediction(
            raw_success_score=member.raw_success_score,
            calibrated_probability=member.calibrated_probability,
            raw_score_frozen_at=raw_score_frozen_at,
            raw_score_training_watermark_at=raw_score_training_watermark_at,
            market_calendar_version=command.market_calendar_version,
            entry_window_ends_at=final_window_session.closes_at,
            entry_sessions=tuple(
                (session.opens_at, session.closes_at)
                for session in window_sessions
                if session is not None
            ),
            matures_by=matures_by,
        )
    return predictions


def _candidate_prediction_month(knowledge_cutoff: datetime) -> str:
    """Keep the cohort month attached to the frozen cutoff's own timezone."""
    return knowledge_cutoff.strftime("%Y-%m")


def _validate_frozen_candidate_prediction_source(
    prediction: _FrozenCandidatePrediction,
    source: RawScoreTrainingRecord,
) -> None:
    """Bind eventual outcome evidence to the previously frozen score and probability."""
    if (
        source.raw_success_score != prediction.raw_success_score
        or source.historical_calibrated_probability != prediction.calibrated_probability
        or source.raw_score_frozen_at != prediction.raw_score_frozen_at
        or source.raw_score_training_watermark_at != prediction.raw_score_training_watermark_at
        or source.entry_window_ends_at != prediction.entry_window_ends_at
    ):
        raise CandidateCalibrationProvenanceInvalid()
    if source.evaluation_entry_at is not None and not any(
        opens_at <= source.evaluation_entry_at <= closes_at
        for opens_at, closes_at in prediction.entry_sessions
    ):
        raise CandidateCalibrationProvenanceInvalid()
    maturity_anchor = source.evaluation_entry_at or prediction.entry_window_ends_at
    if (
        source.market_calendar_version is None
        or source.unified_maturity_at is None
        or source.market_calendar_version != prediction.market_calendar_version
        or source.unified_maturity_at
        != raw_score_maturity_at(maturity_anchor, source.market_calendar_version)
    ):
        raise CandidateCalibrationProvenanceInvalid()


def _matured_candidate_prediction_ids(
    predictions: dict[tuple[str, str, str], _FrozenCandidatePrediction],
    mature_source_rows: dict[tuple[str, str, str], RawScoreTrainingRecord],
    label_watermark_at: datetime,
) -> set[tuple[str, str, str]]:
    """Require source outcome evidence for every prediction mature by this watermark."""
    matured: set[tuple[str, str, str]] = set()
    for identity, prediction in predictions.items():
        source_record = mature_source_rows.get(identity)
        if source_record is None:
            if prediction.matures_by <= label_watermark_at:
                raise CandidateCalibrationProvenanceInvalid()
            continue
        _validate_frozen_candidate_prediction_source(prediction, source_record)
        matured.add(identity)
    return matured


def _retain_frozen_calibration_record(
    rows: dict[tuple[str, str, str], CalibrationRecord], record: CalibrationRecord
) -> None:
    """Preserve previously published cohort evidence without allowing identity rewrites."""
    identity = (record.month, record.security_id, record.research_id)
    previous = rows.setdefault(identity, record)
    if not _same_frozen_calibration_record(previous, record):
        raise CandidateCalibrationProvenanceInvalid()


def _expected_calibration_identities(
    months: tuple[str, ...],
    mature_source_identities: Iterable[tuple[str, str, str]],
    frozen_training_identities: Iterable[tuple[str, str, str]],
    frozen_candidate_prediction_ids: set[tuple[str, str, str]] | None = None,
) -> set[tuple[str, str, str]]:
    """Union mature source labels with identities already frozen by prior calibration."""
    expected = {
        identity
        for identity in (*mature_source_identities, *frozen_training_identities)
        if identity[0] in months
    }
    expected.update(
        identity for identity in frozen_candidate_prediction_ids or () if identity[0] in months
    )
    return expected


def _validate_cutoff_complete_calibration_population(
    months: tuple[str, ...],
    cutoff_mature_source_identities: Iterable[tuple[str, str, str]],
    cutoff_frozen_training_identities: Iterable[tuple[str, str, str]],
    cutoff_matured_candidate_prediction_ids: set[tuple[str, str, str]],
    submitted_identities: set[tuple[str, str, str]],
) -> None:
    """Reject a calibration cohort that omits any row mature at the knowledge cutoff."""
    expected_identities = _expected_calibration_identities(
        months,
        cutoff_mature_source_identities,
        cutoff_frozen_training_identities,
        cutoff_matured_candidate_prediction_ids,
    )
    if submitted_identities != expected_identities:
        raise CandidateCalibrationProvenanceInvalid()


def _retain_immutable_mature_source_row(
    rows: dict[tuple[str, str, str], RawScoreTrainingRecord],
    identity: tuple[str, str, str],
    record: RawScoreTrainingRecord,
) -> None:
    """Keep the first persisted identity and reject any later rewrite of its evidence."""
    previous = rows.setdefault(identity, record)
    if previous != record:
        raise CandidateCalibrationProvenanceInvalid()


def _validate_candidate_qualification_snapshots(
    case: FrozenDecisionCase,
    history: tuple[GovernanceOutcome, ...],
    observed_at: datetime,
) -> tuple[MarketStateQualification, ...]:
    """Bind market-state qualifications to saved, scope-matched governance facts."""
    command = case.candidate_release
    scope = case.access_scope
    assert command is not None and scope is not None
    if len({item.market_state for item in command.qualifications}) != len(command.qualifications):
        raise ValueError("CANDIDATE_MARKET_STATE_QUALIFICATION_DUPLICATE")
    records = tuple(
        outcome.qualification
        for outcome in history
        if outcome.qualification is not None and outcome.qualification.recorded_at <= observed_at
    )
    resolved: list[MarketStateQualification] = []
    for snapshot in command.qualifications:
        record = next(
            (item for item in records if item.decision_id == snapshot.qualification_id), None
        )
        if record is None:
            raise ValueError("CANDIDATE_QUALIFICATION_NOT_IN_LEDGER")
        visible_history = tuple(
            outcome
            for outcome in history
            if outcome.qualification is not None
            and outcome.qualification.recorded_at <= observed_at
        )
        try:
            latest = current_qualification(visible_history, record.scope, record.version)
        except ValueError as error:
            raise CandidateQualificationHistoryAmbiguous() from error
        knowledge_cutoff = datetime.fromisoformat(case.knowledge_cutoff)
        cutoff_history = tuple(
            outcome
            for outcome in history
            if outcome.qualification is not None
            and outcome.qualification.recorded_at <= knowledge_cutoff
        )
        try:
            qualification_at_cutoff = current_qualification(
                cutoff_history, record.scope, record.version
            )
        except ValueError as error:
            raise CandidateQualificationHistoryAmbiguous() from error
        basis = record.formal_passing_evidence or record.authorization_evidence
        if latest is None:
            raise CandidateQualificationHistoryAmbiguous()
        if qualification_at_cutoff is None:
            resolved.append(snapshot.model_copy(update={"status": "NOT_OBTAINED"}))
            continue
        if (
            record.version.version_id != snapshot.capability_version
            or record.version.version_id != command.capability_version
            or record.version.implementation != case.version_bundle
            or record.version.implementation.m_agent_release_commit
            != CURRENT_M_AGENT_RELEASE.m_agent_release_commit
            or latest.version != record.version
        ):
            raise CandidateQualificationVersionMismatch()
        if snapshot.market_calendar_version != command.market_calendar_version or (
            basis is not None and basis.market_calendar_version != command.market_calendar_version
        ):
            raise CandidateQualificationVersionMismatch()
        if (
            record.status != snapshot.status
            or record.recorded_at != snapshot.recorded_at
            or record.scope.market_state != snapshot.market_state
            or record.scope.user_id != scope.user_id
            or set(record.scope.account_ids) != set(scope.account_ids)
            or record.scope.purpose != command.purpose
            or record.scope.target != "SIX_MONTH_TERMINAL_20_PERCENT"
            or record.scope.evidence_level != "D0"
        ):
            raise ValueError("CANDIDATE_QUALIFICATION_SNAPSHOT_MISMATCH")
        if basis is None:
            if (
                record.status != "NOT_OBTAINED"
                or record.authorization_id is not None
                or record.authorization_evidence is not None
                or record.cause != "INSUFFICIENT_EVIDENCE"
                or record.evidence.kind != "INSUFFICIENT_EVIDENCE"
                or record.formal_evidence is not None
                or record.formal_passing_evidence is not None
                or qualification_at_cutoff.decision_id != record.decision_id
                or qualification_at_cutoff.status != "NOT_OBTAINED"
                or snapshot.status != "NOT_OBTAINED"
                or snapshot.valid_through != record.recorded_at
            ):
                raise ValueError("CANDIDATE_QUALIFICATION_SNAPSHOT_MISMATCH")
            resolved.append(snapshot.model_copy(update={"status": "NOT_OBTAINED"}))
            continue
        if basis.expires_at != snapshot.valid_through:
            raise ValueError("CANDIDATE_QUALIFICATION_SNAPSHOT_MISMATCH")
        if (
            record.scope.capability != "candidate-release"
            or record.scope.source != "SYNTHETIC_D0"
            or record.scope.account_type != "SYNTHETIC"
            or record.scope.board != "SYNTHETIC"
            or record.scope.holding_age_domain is not None
            or record.scope.renewal_ordinal is not None
            or record.scope.probability_grid is not None
            or record.scope.portfolio_scope is not None
        ):
            resolved.append(snapshot.model_copy(update={"status": "NOT_OBTAINED"}))
            continue
        cutoff_basis = (
            qualification_at_cutoff.formal_passing_evidence
            or qualification_at_cutoff.authorization_evidence
        )
        latest_basis = latest.formal_passing_evidence or latest.authorization_evidence
        if (
            cutoff_basis is not None
            and cutoff_basis.market_calendar_version != command.market_calendar_version
        ) or (
            latest_basis is not None
            and latest_basis.market_calendar_version != command.market_calendar_version
        ):
            raise CandidateQualificationVersionMismatch()
        if cutoff_basis is None or latest_basis is None:
            resolved.append(snapshot.model_copy(update={"status": "NOT_OBTAINED"}))
            continue
        if qualification_at_cutoff.status not in {
            "VALID",
            "AT_RISK",
        } or not qualification_is_current(qualification_at_cutoff, knowledge_cutoff):
            resolved.append(snapshot.model_copy(update={"status": "NOT_OBTAINED"}))
            continue
        if record.recorded_at > datetime.fromisoformat(case.knowledge_cutoff):
            resolved.append(snapshot.model_copy(update={"status": "NOT_OBTAINED"}))
            continue
        current_basis = latest.formal_passing_evidence or latest.authorization_evidence
        current_status: MarketStateQualificationStatus = latest.status
        if current_status in {"VALID", "AT_RISK"} and not qualification_is_current(
            latest, observed_at
        ):
            current_status = "EXPIRED"
        resolved.append(
            snapshot.model_copy(
                update={
                    "qualification_id": latest.decision_id,
                    "status": current_status,
                    "current_status_recorded_at": latest.recorded_at,
                    "valid_through": (
                        current_basis.expires_at
                        if current_basis is not None
                        else snapshot.valid_through
                    ),
                }
            )
        )
    return tuple(resolved)


def _research_data_gate_results(
    command: ResearchCommand,
    error_code: str | None,
    member_runs: tuple[ResearchMemberRunResult, ...] = (),
) -> tuple[GateResult, ...]:
    """Persist each member/data-type gate when the required-facts Provider fails."""
    member_run_gate_results = tuple(
        GateResult(
            gate_id=f"RESEARCH_RUN:{member.security_id}",
            status=(
                "PASSED"
                if member.status == "SUCCEEDED"
                else "UNKNOWN"
                if member.status == "WAITING"
                else "FAILED"
            ),
        )
        for member in member_runs
    )
    if (
        error_code != "RESEARCH_DATA_UNAVAILABLE"
        and not (error_code or "").startswith("RESEARCH_REQUIRED_FACTS_INCOMPLETE")
        and not member_run_gate_results
    ):
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
        data_failure_member_ids - detailed_manifest_member_ids - invalid_structured_member_ids
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
    execute_research: Callable[[], Awaitable[FrameworkRunResult]] | None = None,
    execute_research_member: ResearchMemberExecutor | None = None,
    record_research_transition: Callable[[FrameworkRunTransition], Awaitable[None]] | None = None,
    execute_risk: Callable[[FrameworkRunResult, ResearchRiskPlan], Awaitable[FrameworkRunResult]],
    record_raw_score_checkpoint: Callable[[ResearchRiskPlan], Awaitable[None]] | None = None,
    legacy: bool = False,
    historical: bool = False,
) -> FrameworkRunResult:
    """Orchestrate typed research handoff and independent risk execution in the host."""
    command = case.research
    assert command is not None
    try:
        if execute_research_member is not None:
            research_run = await execute_research_member_runs(
                case,
                execute_member=execute_research_member,
                record_transition=record_research_transition,
                historical=historical,
            )
        elif execute_research is not None:
            research_run = await execute_research()
        else:
            raise ValueError("a research Run executor is required")
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
    research_run_ids = tuple(member_run.run_id for member_run in research_run.research_member_runs)
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
    if record_raw_score_checkpoint is not None:
        await record_raw_score_checkpoint(risk_plan)
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
            research_raw_score_payload(score, legacy=legacy) for score in output.raw_scores or ()
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
            correction_result = original_report.result.model_copy(
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
            case.monitoring is not None
            or case.universe is not None
            or case.selection is not None
            or case.candidate_release is not None
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

                        async def record_raw_score_checkpoint(
                            risk_plan: ResearchRiskPlan,
                        ) -> None:
                            await _record_raw_score_checkpoint(
                                ledger,
                                execution_case,
                                risk_plan,
                                legacy=(
                                    research_contract_mode_for_case(execution_case) == "legacy"
                                ),
                            )

                        async def execute_research_member(
                            member_index: int,
                            member: ResearchMemberInput,
                            run_id: str,
                        ) -> FrameworkRunResult:
                            return await framework_adapter.execute_research_member_run(
                                execution_case,
                                member_index,
                                member,
                                run_id,
                                record_transition,
                                record_member_run_reservation=(
                                    record_research_member_run_reservation
                                ),
                            )

                        research_mode = research_contract_mode_for_case(execution_case)
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
                                execute_research_member=(
                                    None if research_mode == "legacy" else execute_research_member
                                ),
                                record_research_transition=record_transition,
                                execute_risk=execute_risk,
                                record_raw_score_checkpoint=record_raw_score_checkpoint,
                                legacy=research_mode == "legacy",
                                historical=research_mode == "historical",
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
                gate.gate_id == "RESEARCH_MEMBER_RUN_RESERVED" for gate in stage_result.gate_results
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
                gate_results=(GateResult(gate_id="RESEARCH_MEMBER_RUN_RESERVED", status="PASSED"),),
                reasons=("RESEARCH_MEMBER_RUN_ID_RESERVED",),
            ),
            framework_run_id=framework_run_id,
        )
    return True


def _raw_score_stage_result(
    raw_scores: tuple[RawScore, ...] | None,
    *,
    legacy: bool = False,
) -> StageResult:
    """Build the immutable raw-score stage with its independently saved payload."""
    return StageResult(
        phase="RAW_SCORE",
        status="SUCCEEDED",
        gate_results=(GateResult(gate_id="STRUCTURED_Z20", status="PASSED"),),
        reasons=("RAW_SCORE_FROZEN",),
        raw_score_payloads=(
            tuple(research_raw_score_payload(score, legacy=legacy) for score in raw_scores)
            if raw_scores
            else None
        ),
    )


async def _record_raw_score_checkpoint(
    ledger: DecisionLedger[Transaction],
    case: FrozenDecisionCase,
    risk_plan: ResearchRiskPlan,
    *,
    legacy: bool = False,
) -> None:
    """Persist raw scores before an independent risk Run can start or fail."""
    with ledger.serialize_case_execution() as connection:
        ledger.record_stage_result(
            connection,
            case=case,
            stage_result=_raw_score_stage_result(risk_plan.raw_scores, legacy=legacy),
            framework_run_id=case.framework_run_id,
        )


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
    qualification_observed_at = datetime.fromisoformat(ledger.observed_at().replace("Z", "+00:00"))
    committed_at: str | None = None
    candidate_qualifications: tuple[MarketStateQualification, ...] | None = None
    candidate_version_failure: str | None = None
    candidate_data_failure: str | None = None
    candidate_calibration_failure: str | None = None
    candidate_research_failure: CandidateResearchHandoffUnavailable | None = None
    candidate_model_version: str | None = None
    candidate_initial_calibration = True
    candidate_recent_diagnostic_records: tuple[CalibrationRecord, ...] = ()

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
    if execution_case.candidate_release is not None:
        try:
            if execution_case.access_scope is None:
                raise ValueError("CANDIDATE_ACCESS_SCOPE_MISSING")
            candidate_qualifications = _validate_candidate_qualification_snapshots(
                execution_case,
                ledger.governance_history(connection, execution_case.access_scope),
                qualification_observed_at,
            )
        except CandidateQualificationVersionMismatch:
            candidate_version_failure = "CANDIDATE_QUALIFICATION_VERSION_MISMATCH"
            candidate_qualifications = ()
        except ValueError as error:
            candidate_data_failure = str(error)
            candidate_qualifications = ()
        try:
            candidate_model_version = _validate_candidate_release_source(
                execution_case,
                ledger.get_original_decision_event(
                    execution_case.candidate_release.research_object_id,
                    connection,
                ),
            )
        except (CandidateCalibrationVersionMismatch, CandidateResearchVersionMismatch) as error:
            candidate_version_failure = str(error)
        except CandidateResearchHandoffUnavailable as error:
            candidate_research_failure = error
        except ValueError as error:
            candidate_data_failure = str(error)
        if (
            candidate_research_failure is None
            and candidate_version_failure is None
            and candidate_data_failure is None
        ):
            try:
                assert execution_case.access_scope is not None
                (
                    candidate_initial_calibration,
                    candidate_recent_diagnostic_records,
                ) = _validate_candidate_calibration_sources(
                    execution_case.candidate_release,
                    ledger,
                    connection,
                    execution_case.access_scope,
                    candidate_model_version=candidate_model_version or "",
                )
            except CandidateCalibrationVersionMismatch as error:
                candidate_version_failure = str(error)
            except CandidateCalibrationProvenanceInvalid as error:
                candidate_calibration_failure = str(error)
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
    candidate_release_result: StageResult | None = None
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
            if business_result is not None and execution_case.candidate_release is not None:
                publication_time = datetime.fromisoformat(
                    ledger.observed_at().replace("Z", "+00:00")
                )
                successful_prerequisite = business_result.status == "SUCCEEDED"
                assert candidate_qualifications is not None
                publication_command = execution_case.candidate_release.model_copy(
                    update={"qualifications": candidate_qualifications}
                )
                if not successful_prerequisite:
                    candidate_release = candidate_release_blocked_by_business_prerequisite(
                        publication_command,
                        published_at=publication_time,
                        reason="BUSINESS_PREREQUISITE_NOT_SUCCEEDED",
                    )
                elif candidate_version_failure is not None:
                    candidate_release = candidate_release_availability_failure(
                        publication_command,
                        published_at=publication_time,
                        reason=candidate_version_failure,
                        availability_failure="VERSION",
                    )
                elif candidate_data_failure is not None:
                    candidate_release = candidate_release_availability_failure(
                        publication_command,
                        published_at=publication_time,
                        reason=candidate_data_failure,
                        availability_failure="DATA",
                    )
                elif candidate_research_failure is not None:
                    if candidate_research_failure.disposition == "BLOCKED":
                        candidate_release = candidate_release_blocked_by_business_prerequisite(
                            publication_command,
                            published_at=publication_time,
                            reason=candidate_research_failure.reason,
                        )
                    else:
                        candidate_release = candidate_release_availability_failure(
                            publication_command,
                            published_at=publication_time,
                            reason=candidate_research_failure.reason,
                            availability_failure=(
                                "SYSTEM"
                                if candidate_research_failure.disposition == "SYSTEM_FAILED"
                                else "DATA"
                            ),
                        )
                elif candidate_calibration_failure is not None:
                    candidate_release = candidate_release_availability_failure(
                        publication_command,
                        published_at=publication_time,
                        reason=candidate_calibration_failure,
                        availability_failure="CALIBRATION",
                    )
                else:
                    candidate_release = freeze_candidate_release(
                        publication_command,
                        published_at=publication_time,
                        initial_calibration=candidate_initial_calibration,
                        recent_diagnostic_records=candidate_recent_diagnostic_records,
                    )
                committed_at = ledger.observed_at()
                if (
                    successful_prerequisite
                    and candidate_version_failure is None
                    and candidate_data_failure is None
                    and candidate_calibration_failure is None
                    and candidate_research_failure is None
                ):
                    assert execution_case.access_scope is not None
                    final_publication_time = datetime.fromisoformat(
                        committed_at.replace("Z", "+00:00")
                    )
                    try:
                        final_qualifications = _validate_candidate_qualification_snapshots(
                            execution_case,
                            ledger.governance_history(connection, execution_case.access_scope),
                            final_publication_time,
                        )
                    except CandidateQualificationVersionMismatch as error:
                        publication_command = publication_command.model_copy(
                            update={"qualifications": ()}
                        )
                        candidate_release = candidate_release_availability_failure(
                            publication_command,
                            published_at=final_publication_time,
                            reason=str(error),
                            availability_failure="VERSION",
                        )
                    except ValueError as error:
                        publication_command = publication_command.model_copy(
                            update={"qualifications": ()}
                        )
                        candidate_release = candidate_release_availability_failure(
                            publication_command,
                            published_at=final_publication_time,
                            reason=str(error),
                            availability_failure="DATA",
                        )
                    else:
                        publication_command = publication_command.model_copy(
                            update={"qualifications": final_qualifications}
                        )
                candidate_release = finalize_candidate_release_publication(
                    publication_command,
                    candidate_release,
                    published_at=datetime.fromisoformat(committed_at.replace("Z", "+00:00")),
                )
                result_updates: dict[str, object] = {"candidate_release": candidate_release}
                if successful_prerequisite:
                    result_updates.update(
                        {
                            "outcome_code": f"CANDIDATE_RELEASE_{candidate_release.disposition}",
                            "summary": "Synthetic calibrated candidate release.",
                            "key_reasons": candidate_release.reasons
                            or ("CANDIDATE_RELEASE_FROZEN",),
                        }
                    )
                result = result.model_copy(update=result_updates)
                candidate_status: Literal["FAILED", "REJECTED", "ABSTAINED", "SUCCEEDED"] = (
                    "REJECTED"
                    if not successful_prerequisite
                    else _candidate_release_stage_status(candidate_release.disposition)
                )
                candidate_release_result = _candidate_release_stage_result(
                    candidate_status,
                    candidate_release.reasons,
                )
                if successful_prerequisite:
                    business_result = _candidate_release_stage_result(
                        candidate_status,
                        candidate_release.reasons,
                        phase="BUSINESS_DECISION",
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
    if candidate_release_result is not None:
        ledger.record_stage_result(
            connection,
            case=execution_case,
            stage_result=candidate_release_result,
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
        *((candidate_release_result,) if candidate_release_result is not None else ()),
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
    commit_observed_at = ledger.observed_at()
    if execution_case.candidate_release is not None:
        assert result.candidate_release is not None
        commit_time = datetime.fromisoformat(commit_observed_at.replace("Z", "+00:00"))
        commit_candidate_release = finalize_candidate_release_publication(
            publication_command,
            result.candidate_release,
            published_at=commit_time,
        )
        if commit_candidate_release != result.candidate_release:
            release_content_changed = (
                commit_candidate_release.model_copy(
                    update={"published_at": result.candidate_release.published_at}
                )
                != result.candidate_release
            )
            result = result.model_copy(
                update={
                    "candidate_release": commit_candidate_release,
                    **(
                        {
                            "outcome_code": (
                                f"CANDIDATE_RELEASE_{commit_candidate_release.disposition}"
                            ),
                            "summary": "Synthetic calibrated candidate release.",
                            "key_reasons": commit_candidate_release.reasons,
                        }
                        if release_content_changed
                        else {}
                    ),
                }
            )
            if release_content_changed:
                commit_candidate_status = _candidate_release_stage_status(
                    commit_candidate_release.disposition
                )
                candidate_release_result = _candidate_release_stage_result(
                    commit_candidate_status,
                    commit_candidate_release.reasons,
                )
                business_result = _candidate_release_stage_result(
                    commit_candidate_status,
                    commit_candidate_release.reasons,
                    phase="BUSINESS_DECISION",
                )
                for stage_result in (business_result, candidate_release_result):
                    ledger.record_stage_result(
                        connection,
                        case=execution_case,
                        stage_result=stage_result,
                        framework_run_id=execution_case.framework_run_id,
                        allow_repeated_occurrence=True,
                    )
                stage_results_before_commit = ledger.get_stage_results(
                    execution_case.business_object_id,
                    connection,
                )
                current_stage_results_before_commit = (
                    *current_stage_results_before_commit,
                    business_result,
                    candidate_release_result,
                )
    committed_at = commit_observed_at
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
    raw_score_stage = _raw_score_stage_result(
        envelope.raw_scores,
        legacy=research_mode == "legacy",
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
    candidate_command = fact.case.candidate_release
    candidate_outcome = fact.result.candidate_release
    if (
        candidate_command is not None
        and candidate_outcome is not None
        and fact.corrects_event_id is None
        and candidate_outcome.disposition
        in {"CANDIDATES", "VALID_NO_CANDIDATES", "RECOMMENDATION_ABSTAINED"}
    ):
        confirmed_at = datetime.fromisoformat(ledger.observed_at().replace("Z", "+00:00"))
        assert fact.case.access_scope is not None
        governance_history = ledger.governance_history(connection, fact.case.access_scope)
        diagnostic_alert_ids = _post_commit_diagnostic_alert_ids(
            fact, candidate_command, governance_history
        )
        qualification_history_for_validation = (
            tuple(
                outcome
                for outcome in governance_history
                if outcome.qualification is None
                or outcome.qualification.decision_id not in diagnostic_alert_ids
            )
            if diagnostic_alert_ids
            else governance_history
        )
        try:
            current_qualifications = _validate_candidate_qualification_snapshots(
                fact.case,
                qualification_history_for_validation,
                confirmed_at,
            )
        except (ValueError, CandidateQualificationHistoryAmbiguous):
            failed_report = _record_candidate_publication_failure(
                ledger,
                connection,
                fact,
                "CANDIDATE_QUALIFICATION_CHANGED_BEFORE_PUBLICATION",
            )
            if failed_report is not None:
                return failed_report, None
            return None, DecisionEventCommitError(
                "candidate qualification changed before publication"
            )
        confirmed_outcome = finalize_candidate_release_publication(
            candidate_command.model_copy(update={"qualifications": current_qualifications}),
            candidate_outcome,
            published_at=confirmed_at,
        )
        if (
            candidate_outcome.disposition
            in {"CANDIDATES", "VALID_NO_CANDIDATES", "RECOMMENDATION_ABSTAINED"}
            and confirmed_outcome.disposition == "FAILED"
            and "PUBLICATION_AFTER_CANDIDATE_WINDOW" in confirmed_outcome.reasons
        ):
            failed_report = _record_candidate_publication_failure(
                ledger,
                connection,
                fact,
                "PUBLICATION_AFTER_CANDIDATE_WINDOW",
            )
            if failed_report is not None:
                return failed_report, None
            return None, DecisionEventCommitError("candidate publication window expired")
        outcome_without_qualification_change = confirmed_outcome.model_copy(
            update={
                "published_at": candidate_outcome.published_at,
                "qualification": candidate_outcome.qualification,
            }
        )
        if outcome_without_qualification_change != candidate_outcome:
            failed_report = _record_candidate_publication_failure(
                ledger,
                connection,
                fact,
                "CANDIDATE_QUALIFICATION_CHANGED_BEFORE_PUBLICATION",
            )
            if failed_report is not None:
                return failed_report, None
            return None, DecisionEventCommitError(
                "candidate qualification changed before publication"
            )
    publication_recorded_at = _record_fact_stage_result(
        ledger,
        connection,
        fact,
        report.stage_results[-1],
    )
    if (
        publication_recorded_at is not None
        and candidate_command is not None
        and candidate_outcome is not None
        and fact.corrects_event_id is None
        and candidate_outcome.disposition
        in {"CANDIDATES", "VALID_NO_CANDIDATES", "RECOMMENDATION_ABSTAINED"}
    ):
        recorded_time = datetime.fromisoformat(publication_recorded_at.replace("Z", "+00:00"))
        assert fact.case.access_scope is not None
        current_governance_history = ledger.governance_history(
            connection,
            fact.case.access_scope,
        )
        diagnostic_alert_ids = _post_commit_diagnostic_alert_ids(
            fact,
            candidate_command,
            current_governance_history,
        )
        qualification_history_for_validation = (
            tuple(
                outcome
                for outcome in current_governance_history
                if outcome.qualification is None
                or outcome.qualification.decision_id not in diagnostic_alert_ids
            )
            if diagnostic_alert_ids
            else current_governance_history
        )
        publication_failure_reason: str | None = None
        try:
            current_qualifications = _validate_candidate_qualification_snapshots(
                fact.case,
                qualification_history_for_validation,
                recorded_time,
            )
        except (ValueError, CandidateQualificationHistoryAmbiguous):
            publication_failure_reason = "CANDIDATE_QUALIFICATION_CHANGED_BEFORE_PUBLICATION"
        if publication_failure_reason is None:
            confirmed_outcome = finalize_candidate_release_publication(
                candidate_command.model_copy(update={"qualifications": current_qualifications}),
                candidate_outcome,
                published_at=recorded_time,
            )
            outcome_without_publication_clock = confirmed_outcome.model_copy(
                update={
                    "published_at": candidate_outcome.published_at,
                    "qualification": candidate_outcome.qualification,
                }
            )
            if outcome_without_publication_clock != candidate_outcome:
                publication_failure_reason = (
                    "PUBLICATION_AFTER_CANDIDATE_WINDOW"
                    if "PUBLICATION_AFTER_CANDIDATE_WINDOW" in confirmed_outcome.reasons
                    else "CANDIDATE_QUALIFICATION_CHANGED_BEFORE_PUBLICATION"
                )
        if publication_failure_reason is not None:
            failed_report = _record_candidate_publication_failure(
                ledger,
                connection,
                fact,
                publication_failure_reason,
            )
            if failed_report is not None:
                return failed_report, None
            return None, DecisionEventCommitError("candidate publication eligibility changed")
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


def _record_candidate_publication_failure(
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    fact: DecisionEventFact,
    reason: str,
) -> FormalReport | None:
    """Expose the persisted candidate result only for recognized closed gate failures."""
    _record_publication_failure(ledger, connection, fact, reason)
    report = ledger.get_formal_report_for_event(fact.decision_event_id, connection)
    if report is None or report.report_publication is None:
        return None
    return report if report.report_publication.status == "FAILED" else None


def _post_commit_diagnostic_alert_ids(
    fact: DecisionEventFact,
    command: CandidateReleaseCommand,
    history: tuple[GovernanceOutcome, ...],
) -> tuple[str, ...]:
    """Find the complete post-commit diagnostic chain for the batch's active market state."""
    if not command.qualifications or fact.case.access_scope is None:
        return ()
    records = tuple(
        outcome.qualification for outcome in history if outcome.qualification is not None
    )
    committed_at = datetime.fromisoformat(fact.committed_at)
    candidate_outcome = fact.result.candidate_release
    if candidate_outcome is None or candidate_outcome.qualification is None:
        return ()
    frozen = candidate_outcome.qualification
    if (
        frozen.market_state != candidate_outcome.market_state
        or frozen.market_state != command.market_state
        or frozen.status not in {"VALID", "AT_RISK"}
    ):
        return ()
    frozen_record = next(
        (record for record in records if record.decision_id == frozen.qualification_id), None
    )
    if frozen_record is None:
        return ()
    chain: list[QualificationRecord] = []
    predecessor = frozen_record
    while True:
        successors = tuple(
            record for record in records if record.previous_decision_id == predecessor.decision_id
        )
        if not successors:
            break
        if len(successors) != 1:
            return ()
        successor = successors[0]
        if (
            successor.recorded_at <= committed_at
            or successor.scope != frozen_record.scope
            or successor.version != frozen_record.version
            or successor.authorization_id != frozen_record.authorization_id
            or successor.authorization_terminated_at != frozen_record.authorization_terminated_at
            or successor.formal_evidence != frozen_record.formal_evidence
            or successor.formal_passing_evidence != frozen_record.formal_passing_evidence
            or successor.authorization_evidence != frozen_record.authorization_evidence
            or successor.restrictions != frozen_record.restrictions
            or successor.restoration_evidence != frozen_record.restoration_evidence
            or successor.state_activity_evidence != frozen_record.state_activity_evidence
        ):
            return ()
        added_alerts = successor.alerts[len(predecessor.alerts) :]
        added_closures = successor.alert_closures[len(predecessor.alert_closures) :]
        if (
            successor.alerts[: len(predecessor.alerts)] != predecessor.alerts
            or successor.alert_closures[: len(predecessor.alert_closures)]
            != predecessor.alert_closures
            or any(
                alert.kind != "DIAGNOSTIC_ALERT" or alert.available_at <= committed_at
                for alert in added_alerts
            )
            or any(closure.evidence.available_at <= committed_at for closure in added_closures)
        ):
            return ()
        if successor.status == "AT_RISK" and successor.cause == "DIAGNOSTIC_ALERT":
            if not (added_alerts or added_closures) or not successor.outstanding_alerts:
                return ()
        elif successor.status == "VALID" and successor.cause == "ALERT_CLOSED":
            resolved_alert_ids = {
                closure.alert_evidence_id
                for closure in successor.alert_closures
                if closure.terminates_alert
            }
            if (
                not added_closures
                or not predecessor.outstanding_alerts
                or not {alert.evidence_id for alert in predecessor.outstanding_alerts}.issubset(
                    resolved_alert_ids
                )
                or successor.outstanding_alerts
            ):
                return ()
        else:
            return ()
        chain.append(successor)
        predecessor = successor
    if not chain:
        return ()
    try:
        current = current_qualification(history, predecessor.scope, predecessor.version)
    except ValueError:
        return ()
    if current is not None and current.decision_id == predecessor.decision_id:
        return tuple(record.decision_id for record in chain)
    return ()


_CANDIDATE_RELEASE_STAGE_STATUSES: dict[
    str, Literal["FAILED", "REJECTED", "ABSTAINED", "SUCCEEDED"]
] = {
    "FAILED": "FAILED",
    "BLOCKED": "REJECTED",
    "RECOMMENDATION_ABSTAINED": "ABSTAINED",
    "CANDIDATES": "SUCCEEDED",
    "VALID_NO_CANDIDATES": "SUCCEEDED",
}


def _candidate_release_stage_status(
    disposition: str,
) -> Literal["FAILED", "REJECTED", "ABSTAINED", "SUCCEEDED"]:
    return _CANDIDATE_RELEASE_STAGE_STATUSES[disposition]


def _candidate_release_stage_result(
    status: Literal["FAILED", "REJECTED", "ABSTAINED", "SUCCEEDED"],
    reasons: tuple[str, ...],
    *,
    phase: Literal["CANDIDATE_RELEASE", "BUSINESS_DECISION"] = "CANDIDATE_RELEASE",
) -> StageResult:
    gate_status: Literal["FAILED", "PASSED"] = (
        "FAILED" if status in {"FAILED", "REJECTED"} else "PASSED"
    )
    return StageResult(
        phase=phase,
        status=status,
        gate_results=(
            GateResult(
                gate_id=(
                    "CANDIDATE_RELEASE_OUTCOME"
                    if phase == "BUSINESS_DECISION"
                    else "CALIBRATED_CANDIDATE_RELEASE"
                ),
                status=gate_status,
            ),
        ),
        reasons=reasons,
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
    recorded_at: str | None = None,
) -> str | None:
    """Append event evidence with its selected controlled observation timestamp."""
    return ledger.record_stage_result(
        connection,
        case=fact.case,
        stage_result=stage_result,
        decision_event_id=fact.decision_event_id,
        framework_run_id=fact.framework_run_id,
        allow_repeated_occurrence=allow_repeated_occurrence,
        recorded_at=recorded_at,
    )


def _published_execution(report: FormalReport) -> DecisionCaseExecution:
    publication_status: Literal["PUBLISHED", "FAILED"] = (
        report.report_publication.status if report.report_publication is not None else "PUBLISHED"
    )
    return DecisionCaseExecution(
        business_object_id=report.business_object_id,
        framework_run_id=report.framework_run_id,
        decision_event_id=report.event_id,
        report_version_id=report.report_version_id,
        framework_run_status=_framework_run_status_from_stage_history(report.stage_results),
        business_result_status=_business_result_status_from_stages(report.stage_results),
        business_lifecycle=_business_lifecycle_from_stages(report.stage_results),
        business_commit_status="COMMITTED",
        publication_status=publication_status,
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
