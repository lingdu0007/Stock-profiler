"""Resolve standard evaluation from retained source events at the frozen case seam."""

from datetime import datetime
from zoneinfo import ZoneInfo

from stock_profiler.modules.decision_cases.domain import DecisionEventFact, FrozenDecisionCase
from stock_profiler.modules.decision_cases.evaluation_registration import (
    register_evaluation_members,
)
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.evaluation.contracts import (
    CandidateBatchDelivery,
    EvaluationMember,
    EvaluationRegistration,
    StandardObservation,
    StandardOutcomeReport,
)
from stock_profiler.modules.evaluation.service import (
    evaluate_member,
    population_counts,
    validate_evidence_revision,
)
from stock_profiler.modules.portfolio.market_calendar import (
    six_month_terminal_evaluation_at,
    synthetic_market_calendar,
)


def assess_standard_outcomes(
    case: FrozenDecisionCase, ledger: DecisionLedger[Transaction], connection: Transaction
) -> StandardOutcomeReport:
    command = case.standard_outcomes
    assert command is not None and case.access_scope is not None
    source = ledger.get_decision_event(command.source_event_id, connection)
    if (
        source is None
        or source.case.access_scope is None
        or not source.case.access_scope.same_scope_as(case.access_scope)
        or source.corrects_event_id is not None
        or (command.selection_event_id is not None and source.result.selection is None)
        or (command.candidate_event_id is not None and source.case.candidate_release is None)
        or datetime.fromisoformat(source.case.knowledge_cutoff) > command.cutoff_at
        or datetime.fromisoformat(source.committed_at) > command.cutoff_at
    ):
        raise ValueError("STANDARD_SELECTION_SOURCE_UNAVAILABLE")
    selection = source.result.selection
    source_registrations = retained_registrations(source)
    if command.candidate_event_id and source_registrations:
        raise ValueError("STANDARD_SELECTION_SOURCE_REQUIRED")
    source_members = selection.members if selection else ()
    source_cutoff = (
        selection.cutoff_at if selection else datetime.fromisoformat(source.case.knowledge_cutoff)
    )
    calendar = synthetic_market_calendar(command.market_calendar_version)
    if calendar is None:
        raise ValueError("STANDARD_CALENDAR_UNAVAILABLE")
    sessions = calendar.sessions_after_open(source_cutoff, 5)
    if len(sessions) != 5:
        raise ValueError("STANDARD_ENTRY_WINDOW_UNAVAILABLE")
    maturity = six_month_terminal_evaluation_at(sessions[-1].closed_at, calendar.version_id)
    history = tuple(
        event
        for event in ledger.standard_evaluation_history(connection, case.access_scope)
        if event.result.standard_outcomes is not None
        and event.case.standard_outcomes is not None
        and event.case.standard_outcomes.source_event_id == command.source_event_id
    )
    previous = history[-1] if history else None
    if command.previous_event_id != (previous.decision_event_id if previous else None):
        raise ValueError("STANDARD_REPORT_LINEAGE_CONFLICT")
    prior_report = previous.result.standard_outcomes if previous else None
    if previous is not None:
        previous_command = previous.case.standard_outcomes
        assert previous_command is not None and prior_report is not None
        if (
            command.cutoff_at < prior_report.cutoff_at
            or command.cutoff_at < datetime.fromisoformat(previous.committed_at)
            or command.standard_quantity != previous_command.standard_quantity
            or command.market_calendar_version != previous_command.market_calendar_version
        ):
            raise ValueError("STANDARD_REPORT_CONTRACT_CHANGED")
    previous_formal = (
        ledger.get_formal_report_for_event(previous.decision_event_id, connection)
        if previous
        else None
    )
    retained = (
        {
            member.security_id: member.observation
            for member in prior_report.members
            if member.observation is not None
        }
        if prior_report
        else {}
    )
    evidence_by_id = {
        evidence.evidence_id: evidence
        for event in history
        if event.result.standard_outcomes is not None
        for member in event.result.standard_outcomes.members
        if member.observation is not None
        for evidence in (member.observation.entry, member.observation.terminal)
        if evidence is not None
    }
    observations = {item.security_id: item for item in command.observations}
    if len(observations) != len(command.observations) or set(observations) - set(source_members):
        raise ValueError("STANDARD_OBSERVATION_MEMBERSHIP_INVALID")
    for security, incoming in observations.items():
        prior = retained.get(security)
        for evidence, old in (
            (incoming.entry, prior.entry if prior else None),
            (incoming.terminal, prior.terminal if prior else None),
        ):
            if evidence is not None:
                validate_evidence_revision(evidence, old, evidence_by_id, command.cutoff_at)
                evidence_by_id[evidence.evidence_id] = evidence
        retained[security] = StandardObservation(
            security_id=security,
            entry=incoming.entry or (prior.entry if prior else None),
            terminal=incoming.terminal or (prior.terminal if prior else None),
        )
    members = tuple(
        evaluate_member(
            EvaluationMember(
                evaluation_id=registration.evaluation_id,
                population=registration.population,
                source_event_id=registration.source_event_id,
                security_id=registration.security_id,
                inclusion_reason=registration.admission_reason,
                frozen_probability=registration.frozen_probability,
                matures_at=maturity,
                state="UNAVAILABLE" if maturity <= command.cutoff_at else "PENDING",
            ),
            retained.get(registration.security_id),
            tuple(session.closed_at for session in sessions),
            command.cutoff_at,
            command.standard_quantity,
            command.market_calendar_version,
        )
        for registration in source_registrations
        if registration.population == "SELECTION"
    )
    releases = tuple(
        event
        for event in ledger.candidate_release_history(connection, case.access_scope)
        if event.case.candidate_release is not None
        and (
            event.decision_event_id == command.candidate_event_id
            if command.candidate_event_id
            else event.case.candidate_release.knowledge_cutoff == source_cutoff
        )
        and datetime.fromisoformat(event.committed_at) <= command.cutoff_at
    )
    if command.selection_event_id is not None:
        releases = tuple(
            event
            for event in releases
            if release_belongs_to_selection(event, source, ledger, connection, command.cutoff_at)
        )
    for event in (source, *releases):
        for registration in retained_registrations(event):
            if (
                registration.standard_quantity != command.standard_quantity
                or registration.market_calendar_version != command.market_calendar_version
            ):
                raise ValueError("STANDARD_REGISTERED_POLICY_MISMATCH")
    population_members = list(members) if selection else []
    deliveries = []
    for event in releases:
        for registration in retained_registrations(event):
            if registration.security_id not in source_members:
                raise ValueError("STANDARD_PROBABILITY_MEMBERSHIP_INVALID")
            base = next(
                member for member in members if member.security_id == registration.security_id
            )
            population_members.append(
                base.model_copy(
                    update={
                        "evaluation_id": registration.evaluation_id,
                        "population": registration.population,
                        "source_event_id": registration.source_event_id,
                        "frozen_probability": registration.frozen_probability,
                        "inclusion_reason": registration.admission_reason,
                    }
                )
            )
        deliveries.append(candidate_batch_delivery(event, case, ledger, connection))
        correction = ledger.get_correction_event(event.decision_event_id, connection)
        if (
            correction is not None
            and datetime.fromisoformat(correction.committed_at) <= command.cutoff_at
        ):
            deliveries.append(candidate_batch_delivery(correction, case, ledger, connection))
    if len({member.evaluation_id for member in population_members}) != len(population_members):
        raise ValueError("STANDARD_POPULATION_IDENTITY_CONFLICT")
    if prior_report and not {member.evaluation_id for member in prior_report.members}.issubset(
        member.evaluation_id for member in population_members
    ):
        raise ValueError("STANDARD_POPULATION_MEMBER_REMOVAL")
    return StandardOutcomeReport(
        selection_event_id=command.selection_event_id,
        candidate_event_id=command.candidate_event_id,
        cutoff_at=command.cutoff_at,
        previous_event_id=command.previous_event_id,
        previous_report_id=previous_formal.report_version_id if previous_formal else None,
        report_version=prior_report.report_version + 1 if prior_report else 1,
        members=tuple(population_members),
        populations={
            population: population_counts(
                tuple(member for member in population_members if member.population == population),
                command.cutoff_at,
            )
            for population in ("SELECTION", "PROBABILITY", "CANDIDATE")
        },
        delivery=tuple(deliveries),
    )


def candidate_batch_delivery(
    event: DecisionEventFact,
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> CandidateBatchDelivery:
    command = event.case.candidate_release
    evaluation_command = case.standard_outcomes
    assert command is not None and evaluation_command is not None
    cutoff = evaluation_command.cutoff_at
    release = event.result.candidate_release
    report = ledger.get_formal_report_for_event(event.decision_event_id, connection)
    correction = ledger.get_correction_event(event.decision_event_id, connection)
    if correction and datetime.fromisoformat(correction.committed_at) > cutoff:
        correction = None
    window_sessions = tuple(
        session
        for session in command.market_sessions
        if release is not None and session.market_date in release.valid_market_dates
    )
    valid_from = window_sessions[0].opens_at if window_sessions else None
    valid_through = window_sessions[-1].closes_at if window_sessions else None
    publication = report.report_publication if report else None
    published = (
        datetime.fromisoformat(publication.published_at)
        if publication and publication.published_at
        else None
    )
    if published is not None and published > cutoff:
        published = None
    reminders = tuple(
        stage.candidate_reminder
        for stage in ledger.get_stage_results(event.business_object_id, connection)
        if stage.candidate_reminder is not None
        and report is not None
        and stage.candidate_reminder.report_version_id == report.report_version_id
        and stage.candidate_reminder.recorded_at <= cutoff
    )
    return CandidateBatchDelivery(
        event_id=event.decision_event_id,
        report_version_id=report.report_version_id if report and published else None,
        plan_month=command.knowledge_cutoff.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m"),
        disposition=release.disposition if release else "FAILED",
        knowledge_cutoff=command.knowledge_cutoff,
        generated_at=datetime.fromisoformat(event.generated_at or event.case.report_generated_at),
        committed_at=datetime.fromisoformat(event.committed_at),
        published_at=published,
        valid_from=valid_from,
        valid_through=valid_through,
        expired=cutoff > valid_through if valid_through is not None else None,
        corrects_event_id=event.corrects_event_id,
        superseded_by_event_id=correction.decision_event_id if correction else None,
        reminders=reminders,
    )


def retained_registrations(event: DecisionEventFact) -> tuple[EvaluationRegistration, ...]:
    """Read saved admission decisions; derive only for the legacy source contract."""
    if event.result.evaluation_registrations:
        return event.result.evaluation_registrations
    return register_evaluation_members(
        event.case, event.decision_event_id, event.result
    ).evaluation_registrations


def release_belongs_to_selection(
    release: DecisionEventFact,
    selection: DecisionEventFact,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    cutoff: datetime,
) -> bool:
    command = release.case.candidate_release
    assert command is not None
    research = ledger.get_decision_event(command.research_event_id, connection)
    if research is None or research.case.research is None:
        if retained_registrations(release):
            raise ValueError("STANDARD_RELEASE_LINEAGE_UNAVAILABLE")
        return True  # Failed monthly attempts have no invented evaluation members.
    if (
        research.case.access_scope is None
        or selection.case.access_scope is None
        or not research.case.access_scope.same_scope_as(selection.case.access_scope)
        or research.business_object_id != command.research_object_id
        or datetime.fromisoformat(research.committed_at) > cutoff
    ):
        raise ValueError("STANDARD_RELEASE_LINEAGE_INVALID")
    return research.case.research.selection_event_id == selection.decision_event_id
