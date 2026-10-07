"""Resolve original population links from scoped, immutable host ledger facts."""

from datetime import datetime

from stock_profiler.foundation.decision_versions import DecisionCaseVersionBundle
from stock_profiler.modules.decision_cases.domain import DecisionEventFact, FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.prospective.contracts import (
    BatchPopulation,
    CycleRegistration,
    MonthlyObservation,
)


def _source(
    identity: str,
    bundle: DecisionCaseVersionBundle,
    case: FrozenDecisionCase,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
    deadline: datetime,
) -> DecisionEventFact:
    event = ledger.get_decision_event(identity, connection)
    if (
        event is None
        or event.case.access_scope is None
        or case.access_scope is None
        or not event.case.access_scope.same_scope_as(case.access_scope)
        or event.corrects_event_id is not None
        or event.case.version_bundle != bundle
        or datetime.fromisoformat(event.committed_at) > deadline
    ):
        raise ValueError("PROSPECTIVE_BATCH_SOURCE_UNAVAILABLE")
    return event


def resolve_populations(
    case: FrozenDecisionCase,
    registration: CycleRegistration,
    observations: dict[str, MonthlyObservation],
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> tuple[BatchPopulation, ...]:
    policy = registration.population_policy
    assert policy is not None and case.prospective is not None and case.access_scope is not None
    cutoff = case.prospective.cutoff_at
    history = ledger.standard_evaluation_history(connection, case.access_scope)
    rows: list[BatchPopulation] = []
    for plan in registration.plan_nodes:
        row = observations.get(plan.plan_month)
        if row is None or row.status not in {"CANDIDATES", "ABSTAINED"}:
            if row is not None and row.batch_link is not None:
                raise ValueError("PROSPECTIVE_FAILED_BATCH_CANNOT_REGISTER_MEMBERS")
            continue
        if row.batch_link is None:
            raise ValueError("PROSPECTIVE_ORIGINAL_BATCH_LINK_REQUIRED")
        selection = _source(
            row.batch_link.selection_event_id,
            policy.selection_bundle,
            case,
            ledger,
            connection,
            row.completed_at,
        )
        candidate = _source(
            row.batch_link.candidate_event_id,
            policy.candidate_bundle,
            case,
            ledger,
            connection,
            row.completed_at,
        )
        selected = selection.result.selection
        release = candidate.result.candidate_release
        command = candidate.case.candidate_release
        if (
            selected is None
            or selected.disposition != "FROZEN"
            or selection.case.selection is None
            or selection.case.selection.cutoff_at != plan.knowledge_cutoff
            or selection.case.selection.policy != selected.policy
            or not selected.population.valid_monthly
            or selected.cutoff_at != plan.knowledge_cutoff
            or len(selected.members) != policy.cohort_size
            or release is None
            or command is None
            or not release.population.valid_monthly
            or release.disposition
            not in {"CANDIDATES", "VALID_NO_CANDIDATES", "RECOMMENDATION_ABSTAINED"}
            or release.knowledge_cutoff != plan.knowledge_cutoff
            or command.knowledge_cutoff != plan.knowledge_cutoff
            or command.capability_version != registration.capability_version
            or command.market_calendar_version != registration.calendar_version
            or release.capability_version != registration.capability_version
            or release.market_calendar_version != registration.calendar_version
            or not plan.knowledge_cutoff <= release.published_at <= plan.disclosure_at
            or (row.status == "CANDIDATES") != any(member.candidate for member in release.members)
            or row.batch_saved_at != datetime.fromisoformat(candidate.committed_at)
        ):
            raise ValueError("PROSPECTIVE_BATCH_CONTRACT_MISMATCH")
        research = _source(
            command.research_event_id,
            policy.research_bundle,
            case,
            ledger,
            connection,
            datetime.fromisoformat(candidate.committed_at),
        )
        if (
            research.case.research is None
            or research.case.research.knowledge_cutoff != plan.knowledge_cutoff
            or research.case.research.selection_event_id != selection.decision_event_id
            or research.business_object_id != command.research_object_id
            or release.research_event_id != research.decision_event_id
            or release.research_object_id != research.business_object_id
        ):
            raise ValueError("PROSPECTIVE_BATCH_LINEAGE_MISMATCH")
        if (
            selected.policy != policy.selection_policy
            or selection.case.selection.strategy_version != policy.selection_strategy_version
            or research.case.research.raw_score_model.model_version
            != policy.raw_score_model_version
            or command.calibrator_version != policy.calibrator_version
            or release.calibration is None
            or release.calibration.calibrator_version != policy.calibrator_version
        ):
            raise ValueError("PROSPECTIVE_BATCH_MODEL_LOCK_MISMATCH")
        admissions = (
            *selection.result.evaluation_registrations,
            *candidate.result.evaluation_registrations,
        )
        cohort = set(selected.members)
        probabilities = {item.security_id: item.calibrated_probability for item in release.members}
        if (
            len(cohort) != policy.cohort_size
            or {item.security_id for item in admissions if item.population == "SELECTION"} != cohort
            or {item.security_id for item in admissions if item.population == "PROBABILITY"}
            != cohort
            or len({item.evaluation_id for item in admissions}) != len(admissions)
            or len(probabilities) != policy.cohort_size
            or sum(item.population == "SELECTION" for item in admissions) != policy.cohort_size
            or sum(item.population == "PROBABILITY" for item in admissions) != policy.cohort_size
            or any(
                item.security_id not in cohort
                or item.source_event_id
                != (
                    selection.decision_event_id
                    if item.population == "SELECTION"
                    else candidate.decision_event_id
                )
                or item.registered_at != plan.knowledge_cutoff
                or item.standard_quantity != policy.standard_quantity
                or item.market_calendar_version != registration.calendar_version
                or (
                    item.population in {"CANDIDATE", "PROBABILITY"}
                    and item.frozen_probability != probabilities.get(item.security_id)
                )
                for item in admissions
            )
        ):
            raise ValueError("PROSPECTIVE_ORIGINAL_POPULATION_INCOMPLETE")
        standards = tuple(
            event
            for event in history
            if event.case.version_bundle == policy.standard_bundle
            and event.result.standard_outcomes is not None
            and event.result.standard_outcomes.selection_event_id == selection.decision_event_id
            and event.result.standard_outcomes.cutoff_at <= cutoff
            and event.case.standard_outcomes is not None
            and event.case.standard_outcomes.market_calendar_version
            == registration.calendar_version
            and event.case.standard_outcomes.standard_quantity == policy.standard_quantity
            and datetime.fromisoformat(event.committed_at) <= cutoff
            and event.corrects_event_id is None
        )
        latest = standards[-1] if standards else None
        report = latest.result.standard_outcomes if latest else None
        rows.append(
            BatchPopulation(
                plan_month=plan.plan_month,
                registrations=admissions,
                members=report.members if report else (),
                standard_event_id=latest.decision_event_id if latest else None,
            )
        )
    return tuple(rows)
