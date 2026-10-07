"""Project complete original populations without advancing unavailable labels."""

from datetime import datetime
from decimal import Decimal

from stock_profiler.modules.evaluation.contracts import EvaluationMember, EvaluationRegistration
from stock_profiler.modules.prospective.contracts import (
    BatchPopulation,
    CycleRegistration,
    CycleWatermark,
    MaturityPolicy,
    PlanNode,
)


def _matches(plan: PlanNode, admission: EvaluationRegistration, member: EvaluationMember) -> bool:
    return (
        member.population == admission.population
        and member.source_event_id == admission.source_event_id
        and member.security_id == admission.security_id
        and member.frozen_probability == admission.frozen_probability
        and admission.registered_at == plan.knowledge_cutoff
    )


def _known_members(plan: PlanNode, row: BatchPopulation) -> dict[str, EvaluationMember]:
    admissions = {item.evaluation_id: item for item in row.registrations}
    outcomes = {item.evaluation_id: item for item in row.members}
    if (
        row.standard_event_id is None
        or len(admissions) != len(row.registrations)
        or len(outcomes) != len(row.members)
        or set(outcomes) - set(admissions)
    ):
        return {}
    return {
        identity: member
        for identity, member in outcomes.items()
        if _matches(plan, admissions[identity], member)
    }


def mature_probability_members(
    plan: PlanNode,
    row: BatchPopulation,
    cutoff: datetime,
) -> tuple[EvaluationMember, ...]:
    return tuple(
        member
        for member in _known_members(plan, row).values()
        if member.population == "PROBABILITY"
        and member.frozen_probability is not None
        and member.matures_at <= cutoff
        and member.state in {"ACHIEVED", "NOT_ACHIEVED"}
    )


def missing_high_band_records(
    plan: PlanNode,
    row: BatchPopulation,
    cutoff: datetime,
    threshold: Decimal,
) -> int:
    known = _known_members(plan, row)
    total = 0
    for admission in row.registrations:
        if (
            admission.population != "PROBABILITY"
            or admission.frozen_probability is None
            or admission.frozen_probability < threshold
        ):
            continue
        member = known.get(admission.evaluation_id)
        due = member.matures_at <= cutoff if member is not None else plan.matures_at <= cutoff
        total += due and (member is None or member.state not in {"ACHIEVED", "NOT_ACHIEVED"})
    return total


def _complete(plan: PlanNode, row: BatchPopulation, cutoff: datetime) -> bool:
    if row.selection_abstained:
        return plan.matures_at <= cutoff
    admissions = {item.evaluation_id: item for item in row.registrations}
    outcomes = _known_members(plan, row)
    if (
        not admissions
        or plan.matures_at > cutoff
        or row.standard_event_id is None
        or len(admissions) != len(row.registrations)
        or len(outcomes) != len(row.members)
        or set(admissions) != set(outcomes)
        or not {"SELECTION", "PROBABILITY"}.issubset(item.population for item in row.registrations)
    ):
        return False
    return all(
        member.matures_at <= cutoff and member.state in {"ACHIEVED", "NOT_ACHIEVED"}
        for member in outcomes.values()
    )


def summarize_maturity(
    registration: CycleRegistration,
    policy: MaturityPolicy,
    populations: tuple[BatchPopulation, ...],
    cutoff: datetime,
) -> CycleWatermark:
    rows = {row.plan_month: row for row in populations}
    if len(rows) != len(populations):
        raise ValueError("PROSPECTIVE_DUPLICATE_BATCH_POPULATION")
    planned = {node.plan_month: node for node in registration.plan_nodes}
    if set(rows) - set(planned):
        raise ValueError("PROSPECTIVE_BATCH_PLAN_UNAVAILABLE")
    completed = {month for month, row in rows.items() if _complete(planned[month], row, cutoff)}
    # The calendar windows are fixed from preregistration, including missing months.
    windows = sum(
        all(node.plan_month in completed for node in registration.plan_nodes[offset : offset + 6])
        for offset in range(0, len(registration.plan_nodes) - 5, 6)
    )
    high_band = sum(
        member.frozen_probability is not None
        and member.frozen_probability >= policy.high_band_threshold
        for month, row in rows.items()
        for member in mature_probability_members(planned[month], row, cutoff)
    )
    pending = {
        month
        for month, row in rows.items()
        if max(
            (
                planned[month].matures_at,
                *(member.matures_at for member in _known_members(planned[month], row).values()),
            )
        )
        > cutoff
    }
    requirements = (
        ("MATURE_BATCHES", len(completed), registration.formal_floor.mature_batches),
        ("NONOVERLAPPING_WINDOWS", windows, registration.formal_floor.nonoverlapping_windows),
        ("HIGH_BAND_RECORDS", high_band, registration.formal_floor.high_band_records),
    )
    waiting = tuple(name for name, actual, required in requirements if actual < required)
    return CycleWatermark(
        required=registration.formal_floor,
        mature_batches=len(completed),
        nonoverlapping_windows=windows,
        high_band_records=high_band,
        pending_batches=len(pending),
        due_missing_batches=len(set(rows) - pending - completed),
        observation_reached=(
            len(completed) >= policy.observation_batches and windows >= policy.observation_windows
        ),
        formal_sufficient=not waiting,
        waiting_for=waiting,
    )
