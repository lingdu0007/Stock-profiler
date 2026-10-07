"""Saved admission identities and complete original windows govern maturity."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from test_prospective_accounting import node, registration

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.evaluation.contracts import EvaluationMember, EvaluationRegistration
from stock_profiler.modules.prospective.contracts import BatchPopulation, MaturityPolicy
from stock_profiler.modules.prospective.maturity import summarize_maturity


def population(index: int, *, missing: bool = False) -> BatchPopulation:
    plan = node(index)
    kinds: tuple[Literal["SELECTION", "PROBABILITY"], ...] = ("SELECTION", "PROBABILITY")
    admitted = tuple(
        EvaluationRegistration(
            evaluation_id=f"fictional-{index}-{kind}",
            population=kind,
            source_event_id=f"fictional-source-{index}-{kind}",
            security_id=f"fictional-security-{index}",
            frozen_probability=Decimal(".85") if kind == "PROBABILITY" else None,
            registered_at=plan.knowledge_cutoff,
            admission_reason="FICTIONAL_ORIGINAL_MEMBER",
            market_calendar_version="fictional-calendar-v1",
        )
        for kind in kinds
    )
    return BatchPopulation(
        plan_month=plan.plan_month,
        registrations=admitted,
        members=tuple(
            EvaluationMember(
                evaluation_id=row.evaluation_id,
                population=row.population,
                source_event_id=row.source_event_id,
                security_id=row.security_id,
                frozen_probability=row.frozen_probability,
                matures_at=plan.matures_at,
                state="UNAVAILABLE" if missing else "ACHIEVED",
            )
            for row in admitted
        ),
        standard_event_id=f"fictional-standard-{index}",
    )


def test_twelve_complete_batches_are_observation_only(settings: Settings) -> None:
    policy = registration(settings, 12)
    floor = MaturityPolicy(
        observation_batches=12, observation_windows=2, high_band_threshold=Decimal(".80")
    )
    report = summarize_maturity(
        policy,
        floor,
        tuple(population(index) for index in range(12)),
        datetime(2045, 1, 1, tzinfo=UTC),
    )
    assert report.mature_batches == 12 and report.nonoverlapping_windows == 2
    assert report.high_band_records == 12 and report.observation_reached
    assert not report.formal_sufficient


def test_original_evaluation_periods_prevent_adjacent_six_month_blocks_from_overlapping(
    settings: Settings,
) -> None:
    policy = registration(settings, 42)
    floor = MaturityPolicy(
        observation_batches=12, observation_windows=2, high_band_threshold=Decimal(".80")
    )
    report = summarize_maturity(
        policy,
        floor,
        tuple(population(index) for index in range(42)),
        datetime(2047, 1, 1, tzinfo=UTC),
    )
    assert report.mature_batches == 42
    assert report.nonoverlapping_windows == 6


def test_missing_original_member_never_shrinks_a_complete_batch(settings: Settings) -> None:
    policy = registration(settings, 24)
    floor = MaturityPolicy(
        observation_batches=12, observation_windows=2, high_band_threshold=Decimal(".80")
    )
    rows = tuple(population(index, missing=index == 8) for index in range(24))
    report = summarize_maturity(policy, floor, rows, datetime(2045, 1, 1, tzinfo=UTC))
    assert report.mature_batches == 23 and report.due_missing_batches == 1
    assert report.nonoverlapping_windows == 4
    assert not report.formal_sufficient
    trimmed = rows[8].model_copy(update={"members": rows[8].members[:1]})
    again = summarize_maturity(
        policy, floor, (*rows[:8], trimmed, *rows[9:]), datetime(2045, 1, 1, tzinfo=UTC)
    )
    assert again == report


def test_success_state_cannot_mature_a_future_label(settings: Settings) -> None:
    policy = registration(settings, 1)
    floor = MaturityPolicy(
        observation_batches=12, observation_windows=2, high_band_threshold=Decimal(".80")
    )
    report = summarize_maturity(policy, floor, (population(0),), node(0).disclosure_at)
    assert report.pending_batches == 1 and report.mature_batches == 0
    assert report.high_band_records == 0 and not report.observation_reached


def test_early_individual_results_wait_for_the_original_batch_clock(settings: Settings) -> None:
    policy = registration(settings, 1)
    floor = MaturityPolicy(
        observation_batches=12, observation_windows=2, high_band_threshold=Decimal(".80")
    )
    original = population(0)
    early = original.model_copy(
        update={
            "members": tuple(
                item.model_copy(update={"matures_at": node(0).matures_at.replace(day=2)})
                for item in original.members
            )
        }
    )
    before = summarize_maturity(policy, floor, (early,), node(0).matures_at.replace(day=3))
    assert before.mature_batches == 0 and before.pending_batches == 1
    assert before.high_band_records == 1
    after = summarize_maturity(policy, floor, (early,), node(0).matures_at)
    assert after.mature_batches == 1 and after.pending_batches == 0


def test_later_individual_labels_complete_after_both_maturity_clocks(settings: Settings) -> None:
    policy = registration(settings, 1)
    floor = MaturityPolicy(
        observation_batches=12, observation_windows=2, high_band_threshold=Decimal(".80")
    )
    original = population(0)
    later = original.model_copy(
        update={
            "members": tuple(
                item.model_copy(update={"matures_at": node(0).matures_at + timedelta(hours=8)})
                for item in original.members
            )
        }
    )
    before = summarize_maturity(policy, floor, (later,), node(0).matures_at + timedelta(hours=1))
    assert before.mature_batches == before.high_band_records == 0
    assert before.pending_batches == 1 and before.due_missing_batches == 0
    after = summarize_maturity(policy, floor, (later,), datetime(2045, 1, 1, tzinfo=UTC))
    assert after.mature_batches == after.high_band_records == 1
    assert after.due_missing_batches == 0


def test_high_band_keeps_known_individual_outcomes_in_an_incomplete_batch(
    settings: Settings,
) -> None:
    policy = registration(settings, 1)
    floor = MaturityPolicy(
        observation_batches=12, observation_windows=2, high_band_threshold=Decimal(".80")
    )
    original = population(0)
    partial = original.model_copy(
        update={
            "registrations": (
                *original.registrations,
                *(
                    item.model_copy(
                        update={
                            "evaluation_id": item.evaluation_id + "-other",
                            "security_id": "fictional-other-security",
                        }
                    )
                    for item in original.registrations
                ),
            ),
            "members": (
                *original.members,
                *(
                    item.model_copy(
                        update={
                            "evaluation_id": item.evaluation_id + "-other",
                            "security_id": "fictional-other-security",
                            "state": "UNAVAILABLE",
                        }
                    )
                    for item in original.members
                ),
            ),
        }
    )
    report = summarize_maturity(policy, floor, (partial,), datetime(2045, 1, 1, tzinfo=UTC))
    assert report.mature_batches == 0 and report.due_missing_batches == 1
    assert report.high_band_records == 1


def test_predicted_probability_and_population_identity_cannot_be_replaced(
    settings: Settings,
) -> None:
    policy = registration(settings, 1)
    floor = MaturityPolicy(
        observation_batches=12, observation_windows=2, high_band_threshold=Decimal(".80")
    )
    original = population(0)
    altered = original.model_copy(
        update={
            "members": (
                original.members[0],
                original.members[1].model_copy(update={"frozen_probability": Decimal(".99")}),
            )
        }
    )
    report = summarize_maturity(policy, floor, (altered,), datetime(2045, 1, 1, tzinfo=UTC))
    assert report.mature_batches == report.high_band_records == 0
    assert report.due_missing_batches == 1


def test_selection_abstention_retains_a_mature_failed_batch_without_predictions(
    settings: Settings,
) -> None:
    policy = registration(settings, 1)
    floor = MaturityPolicy(
        observation_batches=12, observation_windows=2, high_band_threshold=Decimal(".80")
    )
    abstention = BatchPopulation(
        plan_month=node(0).plan_month,
        registrations=(),
        members=(),
        standard_event_id=None,
        selection_abstained=True,
    )
    report = summarize_maturity(policy, floor, (abstention,), datetime(2045, 1, 1, tzinfo=UTC))
    assert report.mature_batches == 1 and report.high_band_records == 0
    assert report.due_missing_batches == report.pending_batches == 0
