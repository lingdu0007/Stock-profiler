"""Project complete original populations without advancing unavailable labels."""

from datetime import datetime

from stock_profiler.modules.prospective.contracts import (
    BatchPopulation,
    CycleRegistration,
    CycleWatermark,
    MaturityPolicy,
    PlanNode,
)


def _complete(plan: PlanNode, row: BatchPopulation, cutoff: datetime) -> bool:
    admissions = {item.evaluation_id: item for item in row.registrations}
    outcomes = {item.evaluation_id: item for item in row.members}
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
    for identity, admission in admissions.items():
        member = outcomes[identity]
        if (
            member.population != admission.population
            or member.source_event_id != admission.source_event_id
            or member.security_id != admission.security_id
            or member.frozen_probability != admission.frozen_probability
            or admission.registered_at != plan.knowledge_cutoff
            or member.matures_at > plan.matures_at
            or member.state not in {"ACHIEVED", "NOT_ACHIEVED"}
        ):
            return False
    return True


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
        item.population == "PROBABILITY"
        and item.frozen_probability is not None
        and item.frozen_probability >= policy.high_band_threshold
        for month in completed
        for item in rows[month].registrations
    )
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
        pending_batches=sum(planned[month].matures_at > cutoff for month in rows),
        due_missing_batches=sum(
            planned[month].matures_at <= cutoff and month not in completed for month in rows
        ),
        observation_reached=(
            len(completed) >= policy.observation_batches and windows >= policy.observation_windows
        ),
        formal_sufficient=not waiting,
        waiting_for=waiting,
    )
