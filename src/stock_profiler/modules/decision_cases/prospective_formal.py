"""Resolve original formal nodes and saved source evidence before statistical inference."""

from datetime import datetime
from decimal import Decimal
from fractions import Fraction

from stock_profiler.modules.decision_cases.cohort_month import resolve_cohort_month
from stock_profiler.modules.decision_cases.domain import DecisionEventFact, FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.evaluation.historical_contracts import HistoricalMonthResult
from stock_profiler.modules.evaluation.historical_revisions import merge_month_evidence
from stock_profiler.modules.prospective.contracts import (
    BatchPopulation,
    CycleFormalLook,
    CycleRegistration,
    CycleWatermark,
    FormalMonth,
    MonthlyObservation,
)
from stock_profiler.modules.prospective.formal_inference import infer_prospective
from stock_profiler.modules.prospective.formal_nodes import prepare_formal_look
from stock_profiler.modules.prospective.maturity import (
    mature_probability_members,
    missing_high_band_records,
)


def assess_formal_look(
    case: FrozenDecisionCase,
    registration: CycleRegistration,
    registered: DecisionEventFact,
    prior: tuple[DecisionEventFact, ...],
    observations: dict[str, MonthlyObservation],
    populations: tuple[BatchPopulation, ...],
    valid_months: set[str],
    watermark: CycleWatermark,
    ledger: DecisionLedger[Transaction],
    connection: Transaction,
) -> CycleFormalLook:
    command = case.prospective
    assert command is not None and case.access_scope is not None
    looks = tuple(
        event.result.prospective.formal_look
        for event in prior
        if event.result.prospective is not None
        and event.result.prospective.formal_look is not None
        and event.result.prospective.formal_look.node_month is not None
    )
    last = looks[-1] if looks else None
    if command.operation != "CHECK":
        return CycleFormalLook(
            disposition="WAITING_FOR_INFERENCE"
            if watermark.formal_sufficient
            else "WAITING_FOR_MATURITY",
            actual_ordinal=last.actual_ordinal if last else 0,
            alpha_spent=last.alpha_spent if last else Decimal(0),
        )
    policy = registration.formal_policy
    if policy is None:
        raise ValueError("PROSPECTIVE_FORMAL_POLICY_REQUIRED")
    assert command.formal_node_month is not None and registration.population_policy is not None
    node = next(
        (node for node in registration.plan_nodes if node.plan_month == command.formal_node_month),
        None,
    )
    if node is None or command.cutoff_at < node.disclosure_at:
        raise ValueError("PROSPECTIVE_FORMAL_NODE_UNAVAILABLE")
    look = prepare_formal_look(
        policy,
        looks,
        command.formal_node_month,
        watermark,
        missed=command.cutoff_at != node.disclosure_at,
    )
    if look.disposition == "SKIPPED":
        return look
    executed = tuple(
        event.case.prospective
        for event in prior
        if event.case.prospective is not None
        and event.result.prospective is not None
        and event.result.prospective.formal_look is not None
        and event.result.prospective.formal_look.disposition
        in {"PASSED", "FAILED", "INDETERMINATE"}
    )
    if len({month.plan_month for month in command.formal_months}) != len(command.formal_months):
        raise ValueError("PROSPECTIVE_COHORT_EVIDENCE_DUPLICATED")
    inputs = merge_month_evidence(
        tuple((old.cutoff_at, old.formal_months) for old in (*executed, command))
    )
    if set(inputs) - valid_months:
        raise ValueError("PROSPECTIVE_COHORT_EVIDENCE_OUTSIDE_VALID_POPULATION")
    by_month = {row.plan_month: row for row in populations}
    months: list[FormalMonth] = []
    for plan in registration.plan_nodes:
        if plan.knowledge_cutoff > command.cutoff_at:
            continue
        row = by_month.get(plan.plan_month)
        if plan.plan_month not in valid_months or row is None:
            months.append(
                FormalMonth(
                    cohort=HistoricalMonthResult(
                        plan_month=plan.plan_month, disposition="UNAVAILABLE"
                    ),
                    probabilities=(),
                )
            )
            continue
        evidence = inputs.get(plan.plan_month)
        observation = observations[plan.plan_month]
        assert observation.batch_link is not None
        if evidence is None:
            raise ValueError("PROSPECTIVE_COHORT_EVIDENCE_REQUIRED")
        if (
            evidence.selection_event_id != observation.batch_link.selection_event_id
            or evidence.standard_event_id != row.standard_event_id
        ):
            raise ValueError("PROSPECTIVE_COHORT_EVIDENCE_SOURCE_CHANGED")
        cohort = resolve_cohort_month(
            evidence,
            plan.plan_month,
            policy.cohort,
            datetime.fromisoformat(registered.committed_at),
            case.access_scope,
            command.cutoff_at,
            ledger,
            connection,
        )
        if plan.matures_at > command.cutoff_at:
            cohort = cohort.model_copy(update={"batch_pass": None, "drawdown_pass": None})
        terminal_boundary = max((plan.matures_at, *(member.matures_at for member in row.members)))
        months.append(
            FormalMonth(
                cohort=cohort,
                probabilities=mature_probability_members(plan, row, command.cutoff_at),
                batch_due=plan.matures_at <= command.cutoff_at,
                evaluation_start_at=plan.first_entry_at,
                evaluation_end_at=terminal_boundary,
                evaluation_period_mature=terminal_boundary <= command.cutoff_at,
                valid_monthly=True,
                has_candidates=observation.status == "CANDIDATES",
                missing_high_band_records=missing_high_band_records(
                    plan,
                    row,
                    command.cutoff_at,
                    registration.population_policy.maturity.high_band_threshold,
                ),
            )
        )
    inference = infer_prospective(
        tuple(months),
        policy,
        high_band_threshold=registration.population_policy.maturity.high_band_threshold,
        alpha=Fraction(policy.total_alpha) / (look.actual_ordinal * (look.actual_ordinal + 1)),
        seed=registered.case.seed + look.actual_ordinal,
    )
    return look.model_copy(update={"disposition": inference.disposition, "inference": inference})
