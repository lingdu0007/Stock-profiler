"""Resolve immutable probability history at the frozen-case boundary."""

from datetime import datetime
from zoneinfo import ZoneInfo

from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    freeze_candidate_release,
)
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    FrozenDecisionCase,
    StageResult,
)
from stock_profiler.modules.decision_cases.historical_selection import registered_months
from stock_profiler.modules.decision_cases.ports import (
    DecisionLedger,
    ResearchAvailabilityFailureFact,
    SelectionAvailabilityFailureFact,
    Transaction,
)
from stock_profiler.modules.decision_cases.standard_outcomes import retained_registrations
from stock_profiler.modules.evaluation.cohort_metrics import historical_sessions
from stock_profiler.modules.evaluation.historical_baselines import market_regime
from stock_profiler.modules.evaluation.historical_contracts import (
    HistoricalMonthInput,
    SelectionIndexEvidence,
)
from stock_profiler.modules.evaluation.probability_contracts import (
    HistoricalProbabilityReport,
    ProbabilityCounts,
    ProbabilityMember,
    ProbabilityMonth,
    ProbabilityRegistration,
)
from stock_profiler.modules.evaluation.probability_inference import summarize_probability_history
from stock_profiler.modules.portfolio.market_calendar import (
    six_month_terminal_evaluation_at,
    synthetic_market_calendar,
)


class InvalidHistoricalProbabilityRequest(ValueError):
    """Rejected probability evidence command audited outside its rolled-back transaction."""


def assess_historical_probability(
    case: FrozenDecisionCase, ledger: DecisionLedger[Transaction], connection: Transaction
) -> HistoricalProbabilityReport:
    command = case.historical_probability
    assert command is not None and case.access_scope is not None
    history = ledger.historical_probability_history(connection, case.access_scope)
    releases = ledger.candidate_release_history(connection, case.access_scope)
    if command.operation == "REGISTER":
        assert command.registration is not None
        registration = command.registration
        if (
            command.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")
            > registration.start_month
        ):
            raise ValueError("PROBABILITY_PREREGISTRATION_TOO_LATE")
        if any(
            event.result.historical_probability is not None
            and event.result.historical_probability.registration.version_id
            == registration.version_id
            for event in history
        ):
            raise ValueError("PROBABILITY_REGISTRATION_IDENTITY_ALREADY_BOUND")
        if any(
            event.case.candidate_release is not None
            and registration.start_month
            <= event.case.candidate_release.knowledge_cutoff.astimezone(
                ZoneInfo("Asia/Shanghai")
            ).strftime("%Y-%m")
            <= registration.end_month
            and event.case.candidate_release.capability_version == registration.capability_version
            for event in releases
        ):
            raise ValueError("PROBABILITY_RESULTS_PRECEDE_REGISTRATION")
        return HistoricalProbabilityReport(
            disposition="REGISTERED",
            registration=registration,
            registration_event_id=None,
            cutoff_at=command.cutoff_at,
        )
    source = ledger.get_decision_event(command.registration_event_id or "", connection)
    if (
        source is None
        or source.case.access_scope is None
        or not source.case.access_scope.same_scope_as(case.access_scope)
        or source.result.historical_probability is None
        or source.result.historical_probability.disposition != "REGISTERED"
        or source.corrects_event_id is not None
        or datetime.fromisoformat(source.committed_at) > command.cutoff_at
    ):
        raise ValueError("PROBABILITY_PREREGISTRATION_UNAVAILABLE")
    registration = source.result.historical_probability.registration
    previous = next(
        (
            event
            for event in reversed(history)
            if event.case.historical_probability is not None
            and event.case.historical_probability.registration_event_id == source.decision_event_id
        ),
        None,
    )
    if command.previous_event_id != (previous.decision_event_id if previous else None):
        raise ValueError("PROBABILITY_REPORT_LINEAGE_CONFLICT")
    if previous and command.cutoff_at < datetime.fromisoformat(previous.committed_at):
        raise ValueError("PROBABILITY_REPORT_CUTOFF_REWOUND")
    prior_formal = (
        ledger.get_formal_report_for_event(previous.decision_event_id, connection)
        if previous
        else None
    )
    evaluated = ledger.standard_evaluation_history(connection, case.access_scope)
    index_by_source = {
        row.source_event_id: row.index
        for event in history
        if event.case.historical_probability is not None
        and event.case.historical_probability.registration_event_id == source.decision_event_id
        for row in event.case.historical_probability.indices
    }
    for incoming_index in command.indices:
        prior = index_by_source.get(incoming_index.source_event_id)
        if prior is not None and prior != incoming_index.index:
            raise ValueError("PROBABILITY_SELECTION_VISIBLE_INDEX_CHANGED")
        index_by_source[incoming_index.source_event_id] = incoming_index.index
    index_identities: dict[str, SelectionIndexEvidence] = {}
    for evidence in index_by_source.values():
        if (
            evidence.evidence_id in index_identities
            and index_identities[evidence.evidence_id] != evidence
        ):
            raise ValueError("PROBABILITY_INDEX_IDENTITY_ALREADY_BOUND")
        index_identities[evidence.evidence_id] = evidence
    by_month: dict[str, DecisionEventFact] = {}
    for event in releases:
        candidate_command = event.case.candidate_release
        assert candidate_command is not None
        month = candidate_command.knowledge_cutoff.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m"
        )
        if (
            not registration.start_month <= month <= registration.end_month
            or candidate_command.capability_version != registration.capability_version
            or datetime.fromisoformat(event.committed_at) > command.cutoff_at
        ):
            continue
        if (
            event.case.version_bundle != registration.source_version_bundle
            or candidate_command.market_calendar_version != registration.market_calendar_version
        ):
            raise ValueError("PROBABILITY_SOURCE_VERSION_MISMATCH")
        if datetime.fromisoformat(event.committed_at) < datetime.fromisoformat(source.committed_at):
            raise ValueError("PROBABILITY_RESULTS_PRECEDE_REGISTRATION")
        if (
            event.result.candidate_release is not None
            and event.result.candidate_release.calibration is not None
        ):
            validate_source_strategy(event, registration, ledger, connection)
        if month in by_month:
            raise ValueError("PROBABILITY_MONTH_HAS_MULTIPLE_ORIGINAL_PREDICTIONS")
        by_month[month] = event
    if set(index_by_source) - {event.decision_event_id for event in by_month.values()}:
        raise ValueError("PROBABILITY_INDEX_SOURCE_UNAVAILABLE")
    months = tuple(
        resolve_probability_month(
            by_month.get(month),
            month,
            registration,
            command.cutoff_at,
            evaluated,
            index_by_source.get(by_month[month].decision_event_id) if month in by_month else None,
            releases,
        )
        for month in registered_months(registration.start_month, registration.end_month)
    )
    failures = ledger.candidate_availability_failure_history(connection, case.access_scope)
    adjusted = list(months)
    for failed in failures:
        failed_command = failed.case.candidate_release
        assert failed_command is not None
        month = failed_command.knowledge_cutoff.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m"
        )
        if (
            failed.case.version_bundle != registration.source_version_bundle
            or failed_command.capability_version != registration.capability_version
            or datetime.fromisoformat(failed.recorded_at) > command.cutoff_at
        ):
            continue
        for index, row in enumerate(adjusted):
            original = by_month.get(month)
            if row.plan_month == month and (
                original is None
                or datetime.fromisoformat(failed.recorded_at)
                <= datetime.fromisoformat(original.committed_at)
            ):
                adjusted[index] = row.model_copy(
                    update={
                        "disposition": "SYSTEM_FAILED",
                        "valid_monthly": False,
                        "has_candidates": False,
                        "availability_failure": "SYSTEM",
                        "reasons": failed.stage_result.reasons,
                    }
                )
    # A complete selection abstention is a valid zero-recommendation month.
    selections = ledger.historical_selection_history(connection, case.access_scope)
    ignored = {
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    }
    for event in selections:
        selection = event.result.selection
        if (
            selection is None
            or event.case.selection is None
            or selection.disposition != "ABSTAINED"
            or event.case.selection.strategy_version != registration.selection_strategy_version
            or event.case.version_bundle.model_dump(exclude=ignored)
            != registration.source_version_bundle.model_dump(exclude=ignored)
            or datetime.fromisoformat(event.committed_at) > command.cutoff_at
        ):
            continue
        month = selection.cutoff_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")
        if registration.start_month <= month <= registration.end_month and datetime.fromisoformat(
            event.committed_at
        ) < datetime.fromisoformat(source.committed_at):
            raise ValueError("PROBABILITY_RESULTS_PRECEDE_REGISTRATION")
        for index, row in enumerate(adjusted):
            if row.plan_month == month and row.disposition == "MISSING":
                adjusted[index] = row.model_copy(
                    update={
                        "disposition": "SELECTION_ABSTAINED",
                        "source_event_id": event.decision_event_id,
                        "valid_monthly": True,
                        "has_candidates": False,
                    }
                )
    # Saved upstream failures remain availability failures even when a retry abstains.
    upstream_failures: tuple[
        SelectionAvailabilityFailureFact | ResearchAvailabilityFailureFact, ...
    ] = (
        *ledger.selection_availability_failure_history(connection, case.access_scope),
        *ledger.research_availability_failure_history(connection, case.access_scope),
        *(
            ResearchAvailabilityFailureFact(
                case=event.case,
                framework_run_id=event.framework_run_id,
                stage_result=StageResult(
                    phase="RAW_SCORE",
                    status="FAILED",
                    gate_results=(),
                    reasons=event.result.research.reasons,
                ),
                recorded_at=event.committed_at,
            )
            for event in ledger.research_event_history(connection, case.access_scope)
            if event.result.research is not None
            and event.result.research.disposition in {"DATA_FAILED", "SYSTEM_FAILED", "BLOCKED"}
        ),
    )
    for failure in upstream_failures:
        failed_case = failure.case
        if (
            failed_case.version_bundle.model_dump(exclude=ignored)
            != registration.source_version_bundle.model_dump(exclude=ignored)
            or datetime.fromisoformat(failure.recorded_at) > command.cutoff_at
        ):
            continue
        if (
            failed_case.selection is not None
            and failed_case.selection.strategy_version != registration.selection_strategy_version
        ):
            continue
        if (
            failed_case.research is not None
            and failed_case.research.raw_score_model.model_version
            != registration.raw_score_model_version
        ):
            continue
        failed_month = (
            datetime.fromisoformat(failed_case.knowledge_cutoff)
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .strftime("%Y-%m")
        )
        for index, row in enumerate(adjusted):
            original = (
                ledger.get_decision_event(row.source_event_id, connection)
                if row.source_event_id
                else None
            )
            if row.plan_month == failed_month and (
                original is None
                or datetime.fromisoformat(failure.recorded_at)
                <= datetime.fromisoformat(original.committed_at)
            ):
                adjusted[index] = row.model_copy(
                    update={
                        "disposition": "SYSTEM_FAILED",
                        "valid_monthly": False,
                        "has_candidates": False,
                        "availability_failure": "SYSTEM",
                        "reasons": failure.stage_result.reasons,
                    }
                )
    months = tuple(adjusted)
    members = tuple(member for row in months for member in row.members)
    counts = ProbabilityCounts(
        planned=len(months),
        missing_months=sum(row.disposition == "MISSING" for row in months),
        valid_months=sum(row.valid_monthly for row in months),
        recommendation_months=sum(row.valid_monthly and row.has_candidates for row in months),
        availability_failures=sum(row.availability_failure is not None for row in months),
        registered=len(members),
        due=sum(member.matures_at <= command.cutoff_at for member in members),
        evaluable=sum(member.state in {"ACHIEVED", "NOT_ACHIEVED"} for member in members),
        due_missing=sum(member.state == "UNAVAILABLE" for member in members),
        immature=sum(member.state == "PENDING" for member in members),
        high_band=sum(
            member.state in {"ACHIEVED", "NOT_ACHIEVED"}
            and member.frozen_probability >= registration.high_band_threshold
            for member in members
        ),
    )
    inference = summarize_probability_history(months, registration, seed=source.case.seed)
    return HistoricalProbabilityReport(
        disposition=inference.overall.disposition,
        registration=registration,
        registration_event_id=source.decision_event_id,
        cutoff_at=command.cutoff_at,
        previous_event_id=command.previous_event_id,
        previous_report_id=prior_formal.report_version_id if prior_formal else None,
        report_version=previous.result.historical_probability.report_version + 1
        if previous and previous.result.historical_probability
        else 1,
        months=months,
        counts=counts,
        inference=inference,
    )


def validate_source_strategy(
    source: DecisionEventFact,
    registration: ProbabilityRegistration,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> None:
    """Bind the candidate's actual research and selection lineage to its registered rules."""
    candidate = source.case.candidate_release
    assert candidate is not None and source.case.access_scope is not None
    research = ledger.get_decision_event(candidate.research_event_id, connection)
    if (
        research is None
        or research.case.research is None
        or research.business_object_id != candidate.research_object_id
        or research.case.access_scope is None
        or not research.case.access_scope.same_scope_as(source.case.access_scope)
        or research.corrects_event_id is not None
        or datetime.fromisoformat(research.committed_at) > candidate.knowledge_cutoff
        or research.case.research.raw_score_model.model_version
        != registration.raw_score_model_version
    ):
        raise ValueError("PROBABILITY_RESEARCH_SOURCE_UNAVAILABLE")
    selection = ledger.get_decision_event(research.case.research.selection_event_id, connection)
    if (
        selection is None
        or selection.case.selection is None
        or selection.result.selection is None
        or selection.business_object_id != research.case.research.selection_object_id
        or selection.case.access_scope is None
        or not selection.case.access_scope.same_scope_as(source.case.access_scope)
        or selection.corrects_event_id is not None
        or datetime.fromisoformat(selection.committed_at) > candidate.knowledge_cutoff
        or selection.case.selection.cutoff_at > candidate.knowledge_cutoff
    ):
        raise ValueError("PROBABILITY_SELECTION_SOURCE_UNAVAILABLE")
    if (
        selection.case.selection.strategy_version != registration.selection_strategy_version
        or research.case.research.screening.strategy_version
        != registration.selection_strategy_version
    ):
        raise ValueError("PROBABILITY_SOURCE_STRATEGY_MISMATCH")


def resolve_probability_month(
    source: DecisionEventFact | None,
    month: str,
    registration: ProbabilityRegistration,
    cutoff: datetime,
    standard_history: tuple[DecisionEventFact, ...],
    index: SelectionIndexEvidence | None,
    release_history: tuple[DecisionEventFact, ...],
) -> ProbabilityMonth:
    if source is None:
        calendar = synthetic_market_calendar(registration.market_calendar_version)
        if calendar is None:
            raise ValueError("PROBABILITY_CALENDAR_UNAVAILABLE")
        closes = tuple(
            session.closed_at
            for session in historical_sessions(calendar)
            if session.closed_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m") == month
        )
        if not closes:
            raise ValueError("PROBABILITY_MONTH_CALENDAR_UNAVAILABLE")
        expected_cutoff = max(closes)
        entry_sessions = calendar.sessions_after_open(expected_cutoff, 5)
        if len(entry_sessions) != 5:
            raise ValueError("PROBABILITY_ENTRY_WINDOW_UNAVAILABLE")
        maturity = six_month_terminal_evaluation_at(
            entry_sessions[-1].closed_at, calendar.version_id
        )
        return ProbabilityMonth(
            plan_month=month,
            disposition="FUTURE" if expected_cutoff > cutoff else "MISSING",
            cutoff_at=expected_cutoff,
            matures_at=maturity,
            label_mature=maturity <= cutoff,
        )
    command = source.case.candidate_release
    release = source.result.candidate_release
    assert command is not None
    calendar = synthetic_market_calendar(registration.market_calendar_version)
    if calendar is None:
        raise ValueError("PROBABILITY_CALENDAR_UNAVAILABLE")
    sessions = calendar.sessions_after_open(command.knowledge_cutoff, 5)
    if len(sessions) != 5:
        raise ValueError("PROBABILITY_ENTRY_WINDOW_UNAVAILABLE")
    maturity = six_month_terminal_evaluation_at(sessions[-1].closed_at, calendar.version_id)
    if release is None:
        return ProbabilityMonth(
            plan_month=month,
            disposition="SYSTEM_FAILED",
            source_event_id=source.decision_event_id,
            cutoff_at=command.knowledge_cutoff,
            matures_at=maturity,
            label_mature=maturity <= cutoff,
            availability_failure="SYSTEM",
        )
    priors = tuple(
        event
        for event in release_history
        if event.case.candidate_release is not None
        and event.case.candidate_release.capability_version == command.capability_version
        and event.case.candidate_release.market_calendar_version == command.market_calendar_version
        and event.case.version_bundle == source.case.version_bundle
        and datetime.fromisoformat(event.committed_at) < datetime.fromisoformat(source.committed_at)
        and datetime.fromisoformat(event.committed_at) <= command.knowledge_cutoff
        and event.result.candidate_release is not None
        and event.result.candidate_release.calibration is not None
        and event.result.candidate_release.calibration.calibrator_version
        == command.calibrator_version
        and {
            record.raw_score_model_version
            for record in event.case.candidate_release.training_records
        }
        == {registration.raw_score_model_version}
    )
    replayed = freeze_candidate_release(command, initial_calibration=not priors)
    snapshot = release.calibration
    if snapshot is None and any(
        member.calibrated_probability is not None for member in release.members
    ):
        raise ValueError("PROBABILITY_FROZEN_CALIBRATION_PROVENANCE_INVALID")
    if snapshot is not None and (
        any(
            record.raw_score_model_version != registration.raw_score_model_version
            for record in (
                *command.calibrator_selection_records,
                *command.training_records,
                *command.recent_diagnostic_records,
            )
        )
        or snapshot.calibrator_version != registration.calibrator_version
        or snapshot.slope < 0
        or replayed.calibration is None
        or (
            snapshot.intercept,
            snapshot.slope,
            snapshot.training_window_months,
            snapshot.label_watermark_at,
        )
        != (
            replayed.calibration.intercept,
            replayed.calibration.slope,
            replayed.calibration.training_window_months,
            replayed.calibration.label_watermark_at,
        )
        or tuple(
            (m.security_id, m.raw_success_score, m.calibrated_probability) for m in release.members
        )
        != tuple(
            (m.security_id, m.raw_success_score, m.calibrated_probability) for m in replayed.members
        )
    ):
        raise ValueError("PROBABILITY_FROZEN_CALIBRATION_PROVENANCE_INVALID")
    probability_registrations = tuple(
        member for member in retained_registrations(source) if member.population == "PROBABILITY"
    )
    by_security = {member.security_id: member for member in release.members}
    latest = next(
        (
            event
            for event in reversed(standard_history)
            if event.result.standard_outcomes is not None
            and datetime.fromisoformat(event.committed_at) <= cutoff
            and event.result.standard_outcomes.cutoff_at <= cutoff
            and any(
                member.source_event_id == source.decision_event_id
                and member.population == "PROBABILITY"
                for member in event.result.standard_outcomes.members
            )
        ),
        None,
    )
    outcome_by_id = (
        {
            member.evaluation_id: member
            for member in latest.result.standard_outcomes.members
            if member.population == "PROBABILITY"
            and member.source_event_id == source.decision_event_id
        }
        if latest and latest.result.standard_outcomes
        else {}
    )
    members = []
    for admitted in probability_registrations:
        frozen = by_security.get(admitted.security_id)
        if frozen is None or frozen.calibrated_probability != admitted.frozen_probability:
            raise ValueError("PROBABILITY_REGISTERED_PREDICTION_CHANGED")
        outcome = outcome_by_id.get(admitted.evaluation_id)
        if outcome and (
            outcome.frozen_probability != admitted.frozen_probability
            or outcome.source_event_id != source.decision_event_id
        ):
            raise ValueError("PROBABILITY_OUTCOME_IDENTITY_MISMATCH")
        assert admitted.frozen_probability is not None
        member_maturity = outcome.matures_at if outcome else maturity
        members.append(
            ProbabilityMember(
                evaluation_id=admitted.evaluation_id,
                source_event_id=source.decision_event_id,
                security_id=admitted.security_id,
                raw_success_score=frozen.raw_success_score,
                frozen_probability=admitted.frozen_probability,
                matures_at=member_maturity,
                state=(
                    outcome.state
                    if outcome
                    and member_maturity <= cutoff
                    and outcome.state in {"ACHIEVED", "NOT_ACHIEVED"}
                    else "UNAVAILABLE"
                    if member_maturity <= cutoff
                    else "PENDING"
                ),
                entry_expired=outcome.entry_expired if outcome else False,
            )
        )
    regime = None
    if index is not None:
        regime = market_regime(
            HistoricalMonthInput(
                plan_month=month,
                selection_event_id=source.decision_event_id,
                standard_event_id=None,
                observations=(),
                paths=(),
                index=index,
                factors=None,
                future_index=None,
            ),
            command.knowledge_cutoff,
            registration.market_calendar_version,
        )
        if regime != command.market_state:
            raise ValueError("PROBABILITY_SELECTION_VISIBLE_STATE_MISMATCH")
    return ProbabilityMonth(
        plan_month=month,
        disposition=release.disposition,
        source_event_id=source.decision_event_id,
        standard_event_id=latest.decision_event_id if latest else None,
        cutoff_at=command.knowledge_cutoff,
        matures_at=maturity,
        regime=regime,
        label_mature=maturity <= cutoff,
        valid_monthly=release.population.valid_monthly,
        has_candidates=any(member.candidate for member in release.members),
        availability_failure=release.availability_failure,
        members=tuple(members),
        index=index if index is not None else None,
        reasons=() if regime else ("PROBABILITY_SELECTION_VISIBLE_INDEX_REQUIRED",),
    )
