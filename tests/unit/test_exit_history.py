"""Independent synthetic clusters exercise the conjunctive historical decision."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from stock_profiler.modules.evaluation.exit_contracts import (
    ExitCostCurve,
    ExitDecisionLabel,
    ExitForecast,
    ExitPolicy,
    ExitPolicyPath,
    ExitPositionResult,
    GuardedExitProbability,
)
from stock_profiler.modules.evaluation.exit_inference import summarize_exit_history


def policy() -> ExitPolicy:
    return ExitPolicy(
        version_id="fictional-history/1",
        start_month="2032-01",
        end_month="2049-12",
        market_calendar_version="synthetic-market-calendar-v1",
        model_version="fictional-model/1",
        action_version="fictional-actions/1",
        cost_version="fictional-costs/1",
        cost_curve=ExitCostCurve(
            commission_rate=Decimal(0),
            minimum_commission=Decimal(1),
            fee_rate=Decimal(0),
            tax_rate=Decimal(0),
            slippage_rate=Decimal(0),
        ),
        baseline_version="fictional-baselines/1",
        loss_grids=tuple(map(Decimal, (".05", ".10", ".15", ".20"))),
        selected_grid=Decimal(".10"),
        states=("BULL", "BEAR", "SIDEWAYS"),
        bootstrap_repetitions=99,
        confidence=Decimal(".95"),
        block_lengths=(6, 9, 12),
        fixed_reduce_loss=Decimal(".10"),
        fixed_exit_loss=Decimal(".20"),
        trailing_reduce_loss=Decimal(".10"),
        trailing_exit_loss=Decimal(".20"),
        safe_downside=Decimal(".20"),
        risky_downside=Decimal(".40"),
        established_value=Decimal(".70"),
        denied_value=Decimal(".30"),
    )


def synthetic_history() -> tuple[ExitPositionResult, ...]:
    registered = policy()
    positions = []
    targets = ("V60", *(f"D20:{grid}" for grid in registered.loss_grids))
    for index in range(216):
        year, month = 2032 + index // 12, index % 12 + 1
        entry = datetime(year, month, 2, 15, tzinfo=UTC)
        terminal_index = year * 12 + month - 1 + 6
        terminal = datetime(terminal_index // 12, terminal_index % 12 + 1, 2, 15, tzinfo=UTC)
        state = registered.states[(index // 24) % 3]
        for member in range(3):
            identity = f"fictional-lifecycle:{index}:{member}"
            labels = []
            for direction in ("HOLD", "REDUCE", "EXIT"):
                downside = Decimal(0) if direction == "HOLD" else Decimal(1)
                value = Decimal(0) if direction == "EXIT" else Decimal(1)
                dreturn = Decimal("-.25") if downside else Decimal(".1")
                vreturn = Decimal("-.1") if not value else Decimal(".2")
                forecast = ExitForecast(
                    prediction_id=identity + direction,
                    cutoff_at=entry,
                    frozen_at=entry,
                    training_watermark_at=datetime(2031, 12, 1, tzinfo=UTC),
                    model_version=registered.model_version,
                    state=state,
                    data_complete=True,
                    qualified_scope=True,
                    new_evidence_id=identity + direction,
                    downside={
                        str(grid): GuardedExitProbability(
                            point=downside, lower_error=Decimal(0), upper_error=Decimal(0)
                        )
                        for grid in registered.loss_grids
                    },
                    value=GuardedExitProbability(
                        point=value, lower_error=Decimal(0), upper_error=Decimal(0)
                    ),
                    baseline_probabilities={
                        name: {target: Decimal(".5") for target in targets}
                        for name in ("STATE_RATE", "TRANSPARENT")
                    },
                    quantiles={
                        target: vreturn if target == "V60" else dreturn for target in targets
                    },
                    hard_exit=False,
                    hard_evidence_id=None,
                )
                labels.append(
                    ExitDecisionLabel(
                        prediction_id=forecast.prediction_id,
                        cutoff_at=entry,
                        state=state,
                        direction=direction,
                        statistical=True,
                        downside_return=dreturn,
                        value_return=vreturn,
                        upside_capture=Decimal(".8") if value else None,
                        forecast=forecast,
                    )
                )
            paths = {}
            for name, net, drawdown in (
                ("PRIMARY", ".3", ".02"),
                ("HOLD_END", ".1", ".1"),
                ("HARD_ONLY", ".1", ".1"),
                ("FIXED_LOSS", ".2", ".03"),
                ("TRAILING_DRAWDOWN", ".2", ".03"),
            ):
                paths[name] = ExitPolicyPath(
                    current_exposure=Decimal(0),
                    pending_target=Decimal(0),
                    trades=(),
                    daily_wealth=(Decimal(1000), Decimal(1300)),
                    dates=(entry, terminal),
                    net_total_return=Decimal(net),
                    gross_total_return=Decimal(net) + Decimal(".001"),
                    maximum_drawdown=Decimal(drawdown),
                    exit_cost=Decimal(1),
                    turnover=Decimal(1),
                )
            positions.append(
                ExitPositionResult(
                    lifecycle_id=identity,
                    candidate_evaluation_id=identity + ":candidate",
                    source_event_id="fictional-source",
                    security_id="fictional-security",
                    entry_at=entry,
                    entry_price=Decimal(10),
                    matures_at=terminal,
                    current_exposure=Decimal(0),
                    state="EVALUABLE",
                    initial_quantity=Decimal(100),
                    observation=None,
                    policies=paths,
                    decisions=tuple(labels),
                    missing_reasons=(),
                )
            )
    return tuple(positions)


def test_shared_month_blocks_require_every_gate_and_have_reproducible_sampling() -> None:
    history = synthetic_history()
    cutoff = datetime(2051, 1, 1, tzinfo=UTC)
    result = summarize_exit_history(history, policy(), seed=271, cutoff=cutoff)
    assert result.disposition == "PASSED"
    assert all(mark.sufficient for mark in result.watermarks.values())
    assert all(gate.passed for gate in result.gates.values() if gate.applicable)
    assert result.watermarks["overall"].position_count == 648
    assert result.watermarks["overall"].mature_months == 216
    assert set(result.resampling) == {"6", "9", "12"}
    assert all(item["calendar_months"] == 216 for item in result.resampling.values())
    replay = summarize_exit_history(history, policy(), seed=271, cutoff=cutoff)
    assert replay == result
    failed = history[0].model_copy(update={"replay_consistent": False})
    rejected = summarize_exit_history((failed, *history[1:]), policy(), seed=271, cutoff=cutoff)
    assert rejected.disposition == "FAILED"
    assert rejected.gates["overall:replay"].passed is False


def test_duplicate_lifecycle_cannot_inflate_independent_history() -> None:
    history = synthetic_history()[:1]
    with pytest.raises(ValueError, match="EXIT_DUPLICATE_LIFECYCLE"):
        summarize_exit_history(
            (*history, *history),
            policy(),
            seed=271,
            cutoff=datetime(2051, 1, 1, tzinfo=UTC),
        )


def test_due_gap_only_marks_affected_market_scope_indeterminate() -> None:
    position = synthetic_history()[0].model_copy(update={"state": "UNAVAILABLE"})
    result = summarize_exit_history(
        (position,), policy(), seed=271, cutoff=datetime(2051, 1, 1, tzinfo=UTC)
    )
    assert result.disposition == "INDETERMINATE"
    assert result.watermarks["BULL"].disposition == "INDETERMINATE"
    assert result.watermarks["BEAR"].disposition == "INSUFFICIENT"


def test_empty_months_do_not_create_separate_market_episodes() -> None:
    history = synthetic_history()
    # Two BULL samples separated only by missing samples cannot establish two episodes.
    sparse = (history[0], history[6])
    result = summarize_exit_history(
        sparse, policy(), seed=271, cutoff=datetime(2051, 1, 1, tzinfo=UTC)
    )
    assert result.watermarks["BULL"].periods == 1
    assert result.disposition == "INSUFFICIENT"


@pytest.mark.parametrize("selected", ["0.1", "1E-1", "0.100"])
def test_equivalent_decimal_grid_encodings_keep_the_same_direction(selected: str) -> None:
    from stock_profiler.modules.evaluation.exit_paths import statistical_direction

    payload = policy().model_dump(mode="json")
    payload["selected_grid"] = selected
    registered = ExitPolicy.model_validate(payload)
    forecast = synthetic_history()[0].decisions[0].forecast
    assert statistical_direction(forecast, registered) == "HOLD"
