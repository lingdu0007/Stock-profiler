"""Original formal nodes preserve skipped looks and the cumulative alpha sequence."""

from decimal import Decimal

import pytest
from test_historical_inference import policy as historical_policy
from test_prospective_accounting import node, registration

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.evaluation.historical_contracts import CohortEvaluationPolicy
from stock_profiler.modules.prospective.contracts import (
    CycleWatermark,
    FormalPolicy,
)
from stock_profiler.modules.prospective.formal_nodes import prepare_formal_look


def formal_policy() -> FormalPolicy:
    legacy = historical_policy().model_dump()
    cohort = CohortEvaluationPolicy.model_validate(
        {key: legacy[key] for key in CohortEvaluationPolicy.model_fields}
    )
    return FormalPolicy(
        cohort=cohort,
        node_months=(node(29).plan_month, node(35).plan_month, node(41).plan_month),
        increment_batches=6,
        increment_windows=1,
        total_alpha=Decimal(".05"),
        block_lengths=(6, 9, 12),
        bootstrap_repetitions=199,
        inner_repetitions=49,
        overall_pass_floor=Decimal(".8"),
        overall_drawdown_floor=Decimal(".8"),
        calibration_success_floor=Decimal(".8"),
        overconfidence_ceiling=Decimal(".05"),
        regime_pass_floor=Decimal(".5"),
        regime_drawdown_floor=Decimal(".6"),
        regime_batches=30,
        regime_formed=24,
        regime_windows=5,
        regime_periods=2,
        regime_high_band_records=100,
    )


def watermark(settings: Settings, batches: int, windows: int, high: int = 100) -> CycleWatermark:
    return CycleWatermark(
        required=registration(settings).formal_floor,
        mature_batches=batches,
        nonoverlapping_windows=windows,
        high_band_records=high,
        pending_batches=0,
        due_missing_batches=0,
        observation_reached=True,
        formal_sufficient=(high >= 100),
        waiting_for=() if high >= 100 else ("HIGH_BAND_RECORDS",),
    )


def test_skipped_original_node_spends_nothing_and_cannot_be_retried(settings: Settings) -> None:
    policy = formal_policy()
    skipped = prepare_formal_look(policy, (), policy.node_months[0], watermark(settings, 24, 4, 99))
    assert skipped.disposition == "SKIPPED"
    assert skipped.actual_ordinal == 0 and skipped.alpha_spent == skipped.alpha_this == 0
    with pytest.raises(ValueError, match="PROSPECTIVE_FORMAL_NODE_ALREADY_CONSUMED"):
        prepare_formal_look(policy, (skipped,), policy.node_months[0], watermark(settings, 24, 4))
    ready = prepare_formal_look(
        policy, (skipped,), policy.node_months[1], watermark(settings, 24, 4)
    )
    assert ready.disposition == "READY" and ready.actual_ordinal == 1
    assert ready.alpha_this == ready.alpha_spent == Decimal(".025")


def test_each_executed_look_requires_six_new_batches_and_one_window(settings: Settings) -> None:
    policy = formal_policy()
    first = prepare_formal_look(policy, (), policy.node_months[0], watermark(settings, 24, 4))
    checked = first.model_copy(update={"disposition": "FAILED"})
    skipped = prepare_formal_look(
        policy, (checked,), policy.node_months[1], watermark(settings, 29, 5)
    )
    assert skipped.disposition == "SKIPPED" and skipped.actual_ordinal == 1
    assert skipped.alpha_spent == Decimal(".025") and skipped.alpha_this == 0
    second = prepare_formal_look(
        policy, (checked, skipped), policy.node_months[2], watermark(settings, 30, 5)
    )
    assert second.actual_ordinal == 2 and second.alpha_this == Decimal(".05") / 6
    assert second.alpha_spent == Decimal(".05") * 2 / 3
    assert second.mature_batches == 30 and second.nonoverlapping_windows == 5


def test_unregistered_or_skipped_over_nodes_cannot_insert_an_extra_look(settings: Settings) -> None:
    policy = formal_policy()
    with pytest.raises(ValueError, match="PROSPECTIVE_FORMAL_NODE_UNAVAILABLE"):
        prepare_formal_look(policy, (), node(28).plan_month, watermark(settings, 24, 4))
    with pytest.raises(ValueError, match="PROSPECTIVE_FORMAL_NODE_ORDER_CONFLICT"):
        prepare_formal_look(policy, (), policy.node_months[1], watermark(settings, 30, 5))
