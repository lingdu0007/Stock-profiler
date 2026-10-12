"""Conjunctive historical gates with shared complete-month lifecycle resampling."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, localcontext
from hashlib import sha256
from math import log
from random import Random
from typing import Literal

from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    CalibrationDiagnosticObservation,
    CalibrationDiagnostics,
    summarize_probability_calibration,
)
from stock_profiler.modules.evaluation.exit_contracts import (
    ExitDecisionLabel,
    ExitGate,
    ExitInference,
    ExitPolicy,
    ExitPositionResult,
    ExitWatermark,
)
from stock_profiler.modules.evaluation.exit_paths import BASELINES
from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar


@dataclass(frozen=True)
class _GateRule:
    direction: Literal["LOWER", "UPPER", "EXACT"]
    threshold: Decimal
    strict: bool = False


def month_axis(policy: ExitPolicy) -> tuple[str, ...]:
    year, month = map(int, policy.start_month.split("-"))
    last_year, last_month = map(int, policy.end_month.split("-"))
    return tuple(
        f"{index // 12:04}-{index % 12 + 1:02}"
        for index in range(year * 12 + month - 1, last_year * 12 + last_month)
    )


def _log_loss(probability: Decimal, event: bool) -> Decimal:
    p = min(max(float(probability), 1e-15), 1 - 1e-15)
    return Decimal(str(-log(p if event else 1 - p)))


def _target(label: ExitDecisionLabel, name: str) -> tuple[Decimal, bool, Decimal] | None:
    if name == "V60":
        if label.value_return is None or label.forecast.value is None:
            return None
        return label.forecast.value.point, label.value_return > 0, label.value_return
    loss = Decimal(name.split(":")[1])
    prediction = label.forecast.downside.get(str(loss))
    if prediction is None or label.downside_return is None:
        return None
    return prediction.point, label.downside_return <= -loss, label.downside_return


def _rules(policy: ExitPolicy) -> dict[str, _GateRule]:
    rules = {f"net:{name}": _GateRule("LOWER", Decimal(0), True) for name in BASELINES}
    for name in ("HOLD_END", "HARD_ONLY"):
        rules[f"drawdown:{name}"] = _GateRule("UPPER", Decimal(0), True)
        rules[f"drawdown_improved:{name}"] = _GateRule("LOWER", Decimal(".50"))
    for name in ("FIXED_LOSS", "TRAILING_DRAWDOWN"):
        rules[f"drawdown:{name}"] = _GateRule("UPPER", Decimal(".02"))
    rules.update(
        {
            "hold_downside": _GateRule("UPPER", Decimal(".20")),
            "hold_value": _GateRule("LOWER", Decimal(".70")),
            "reduce_downside": _GateRule("LOWER", Decimal(".40")),
            "early_exit": _GateRule("UPPER", Decimal(".30")),
            "upside_capture": _GateRule("LOWER", Decimal(".70")),
            "cost": _GateRule("UPPER", Decimal(".01")),
            "cost_to_gross": _GateRule("UPPER", Decimal(".25")),
            "gross_improvement": _GateRule("LOWER", Decimal(0), True),
            "stability": _GateRule("UPPER", Decimal(".10")),
            "coverage": _GateRule("LOWER", Decimal(".50")),
            "replay": _GateRule("EXACT", Decimal(1)),
            "monotone_actions": _GateRule("EXACT", Decimal(1)),
        }
    )
    for target in ("V60", *(f"D20:{grid}" for grid in policy.loss_grids)):
        for baseline in ("STATE_RATE", "TRANSPARENT"):
            for metric in ("log_loss", "brier"):
                rules[f"increment:{target}:{baseline}:{metric}"] = _GateRule(
                    "LOWER", Decimal(0), True
                )
        # All populated action-critical bands retain separate directional envelopes.
        for band in ("LOW", "MIDDLE", "HIGH"):
            rules[f"overconfidence:{target}:{band}"] = _GateRule("UPPER", Decimal(".05"))
    return rules


def _values(
    position: ExitPositionResult, policy: ExitPolicy, state: str | None
) -> dict[str, list[Decimal]]:
    values: dict[str, list[Decimal]] = {name: [] for name in _rules(policy)}
    entry_state = position.decisions[0].state if position.decisions else None
    if state is None or entry_state == state:
        primary = position.policies.get("PRIMARY")
        if primary and primary.net_total_return is not None and position.state == "EVALUABLE":
            for name in BASELINES:
                baseline_path = position.policies[name]
                assert baseline_path.net_total_return is not None
                values[f"net:{name}"].append(
                    primary.net_total_return - baseline_path.net_total_return
                )
                if f"drawdown:{name}" in values:
                    difference = primary.maximum_drawdown - baseline_path.maximum_drawdown
                    values[f"drawdown:{name}"].append(difference)
                    if f"drawdown_improved:{name}" in values:
                        values[f"drawdown_improved:{name}"].append(Decimal(difference < 0))
            hold = position.policies["HOLD_END"]
            assert primary.gross_total_return is not None and hold.gross_total_return is not None
            initial = position.entry_price * position.initial_quantity
            incremental_cost = max(Decimal(0), primary.exit_cost - hold.exit_cost) / initial
            gross = primary.gross_total_return - hold.gross_total_return
            values["cost"].append(incremental_cost)
            values["gross_improvement"].append(gross)
            values["cost_to_gross"].append(incremental_cost / gross if gross > 0 else Decimal(1))
            sold = tuple(trade.exposure_sold for trade in primary.trades)
            values["monotone_actions"].append(
                Decimal(
                    len(sold) <= 2
                    and primary.turnover <= 1
                    and primary.current_exposure in {Decimal(0), Decimal(".5"), Decimal(1)}
                )
            )
            # Reconstruction from the same retained input is checked by the host before inference.
            values["replay"].append(Decimal(position.replay_consistent))
    for label in position.decisions:
        if state is not None and label.state != state:
            continue
        if label.forecast.data_complete and label.forecast.qualified_scope:
            values["coverage"].append(Decimal(label.statistical))
            values["stability"].append(Decimal(label.reversal_without_evidence))
        if label.upside_capture is not None:
            values["upside_capture"].append(label.upside_capture)
        if label.statistical:
            if label.downside_return is not None:
                event = label.downside_return <= -policy.selected_grid
                if label.direction == "HOLD":
                    values["hold_downside"].append(Decimal(event))
                elif label.direction == "REDUCE":
                    values["reduce_downside"].append(Decimal(event))
            if label.value_return is not None:
                if label.direction == "HOLD":
                    values["hold_value"].append(Decimal(label.value_return > 0))
                elif label.direction == "EXIT":
                    values["early_exit"].append(Decimal(label.value_return > 0))
        if not label.forecast.data_complete:
            continue
        for name in ("V60", *(f"D20:{grid}" for grid in policy.loss_grids)):
            target = _target(label, name)
            if target is None:
                continue
            p, event, _ = target
            band = (
                "LOW"
                if p <= (policy.denied_value if name == "V60" else policy.safe_downside)
                else "HIGH"
                if p >= (policy.established_value if name == "V60" else policy.risky_downside)
                else "MIDDLE"
            )
            # Value overstatement and downside understatement are the unsafe directions.
            error = p - Decimal(event) if band == "HIGH" else Decimal(event) - p
            values[f"overconfidence:{name}:{band}"].append(error)
            for baseline in ("STATE_RATE", "TRANSPARENT"):
                comparison = label.forecast.baseline_probabilities.get(baseline, {}).get(name)
                if comparison is None or not 0 <= comparison <= 1:
                    continue
                values[f"increment:{name}:{baseline}:log_loss"].append(
                    _log_loss(comparison, event) - _log_loss(p, event)
                )
                values[f"increment:{name}:{baseline}:brier"].append(
                    (comparison - Decimal(event)) ** 2 - (p - Decimal(event)) ** 2
                )
    return values


def _watermark(
    positions: tuple[ExitPositionResult, ...], policy: ExitPolicy, state: str | None
) -> ExitWatermark:
    mature = tuple(
        position
        for position in positions
        if position.state == "EVALUABLE"
        and (state is None or (position.decisions and position.decisions[0].state == state))
    )
    months = {position.entry_at.strftime("%Y-%m") for position in mature}
    windows = 0
    previous_end = None
    for position in sorted(mature, key=lambda item: item.entry_at):
        if previous_end is None or position.entry_at >= previous_end:
            windows += 1
            previous_end = position.matures_at
    # Empty sample months cannot manufacture a separate market episode.
    populated = sorted(
        {item.entry_at.strftime("%Y-%m") for item in positions if item.state == "EVALUABLE"}
    )
    periods = sum(
        month in months and (index == 0 or populated[index - 1] not in months)
        for index, month in enumerate(populated)
    )
    targets = ("V60", *(f"D20:{grid}" for grid in policy.loss_grids))
    direction_sets: dict[str, set[str]] = {name: set() for name in ("HOLD", "REDUCE", "EXIT")}
    target_sets: dict[str, set[str]] = {name: set() for name in targets}
    event_sets: dict[str, set[str]] = {name: set() for name in targets}
    nonevent_sets: dict[str, set[str]] = {name: set() for name in targets}
    for position in positions:
        if position.state != "EVALUABLE":
            continue
        for label in position.decisions:
            if state is not None and label.state != state:
                continue
            if label.statistical:
                direction_sets[label.direction].add(position.lifecycle_id)
            if not label.forecast.data_complete:
                continue
            for name in targets:
                target = _target(label, name)
                if target is not None:
                    target_sets[name].add(position.lifecycle_id)
                    (event_sets if target[1] else nonevent_sets)[name].add(position.lifecycle_id)
    overall = state is None
    sufficient = (
        len(months) >= (120 if overall else 30)
        and len(mature) >= (500 if overall else 100)
        and windows >= (20 if overall else 5)
        and (overall or periods >= 2)
        and all(len(values) >= (100 if overall else 30) for values in direction_sets.values())
        and all(len(values) >= (200 if overall else 100) for values in target_sets.values())
        and all(
            len(values) >= (50 if overall else 25)
            for values in (*event_sets.values(), *nonevent_sets.values())
        )
    )
    return ExitWatermark(
        mature_months=len(months),
        position_count=len(mature),
        nonoverlapping_windows=windows,
        periods=periods,
        directions={key: len(value) for key, value in direction_sets.items()},
        target_positions={key: len(value) for key, value in target_sets.items()},
        target_events={key: len(value) for key, value in event_sets.items()},
        target_nonevents={key: len(value) for key, value in nonevent_sets.items()},
        sufficient=sufficient,
        disposition="INSUFFICIENT",
    )


def summarize_exit_history(
    positions: tuple[ExitPositionResult, ...], policy: ExitPolicy, *, seed: int, cutoff: datetime
) -> ExitInference:
    if len({item.lifecycle_id for item in positions}) != len(positions):
        raise ValueError("EXIT_DUPLICATE_LIFECYCLE")
    with localcontext(Context(prec=128)):
        return _summarize_exit_history(positions, policy, seed=seed, cutoff=cutoff)


def _summarize_exit_history(
    positions: tuple[ExitPositionResult, ...], policy: ExitPolicy, *, seed: int, cutoff: datetime
) -> ExitInference:
    axis = month_axis(policy)
    axis = tuple(month for month in axis if month <= cutoff.strftime("%Y-%m"))
    scopes: dict[str, str | None] = {"overall": None, **{state: state for state in policy.states}}
    rules = _rules(policy)
    # Every lifecycle's predictions, outcomes and paired baselines travel together.
    totals = {scope: {name: [(Decimal(0), 0) for _ in axis] for name in rules} for scope in scopes}
    for scope, state in scopes.items():
        for position in positions:
            month = position.entry_at.strftime("%Y-%m")
            if month not in axis:
                continue
            index = axis.index(month)
            for name, values in _values(position, policy, state).items():
                numerator, denominator = totals[scope][name][index]
                totals[scope][name][index] = (
                    numerator + sum(values, Decimal(0)),
                    denominator + len(values),
                )

    def metric(scope: str, name: str, indices: Sequence[int]) -> Decimal | None:
        numerator = sum((totals[scope][name][index][0] for index in indices), Decimal(0))
        denominator = sum(totals[scope][name][index][1] for index in indices)
        return numerator / denominator if denominator else None

    distributions: dict[str, dict[str, list[Decimal]]] = {
        f"{scope}:{name}": {} for scope in scopes for name in rules
    }
    undefined: dict[str, dict[str, int]] = {key: {} for key in distributions}
    resampling: dict[str, dict[str, str | int]] = {}
    for length in policy.block_lengths:
        random = Random(
            int(sha256(f"{policy.version_id}:{seed}:{length}".encode()).hexdigest(), 16)
        )
        label = str(length)
        digest = sha256()
        for key in distributions:
            distributions[key][label] = []
            undefined[key][label] = 0
        for _ in range(policy.bootstrap_repetitions):
            indices: list[int] = []
            block = min(length, len(axis))
            while len(indices) < len(axis):
                start = random.randrange(len(axis) - block + 1)
                indices.extend(range(start, start + block))
            indices = indices[: len(axis)]
            digest.update((",".join(map(str, indices)) + ";").encode())
            for scope in scopes:
                for name in rules:
                    value = metric(scope, name, indices)
                    key = f"{scope}:{name}"
                    if value is None:
                        undefined[key][label] += 1
                    else:
                        distributions[key][label].append(value)
        resampling[label] = {
            "calendar_months": len(axis),
            "repetitions": policy.bootstrap_repetitions,
            "sampling_digest": digest.hexdigest(),
        }
    gates = {}
    for scope in scopes:
        for name, rule in rules.items():
            key = f"{scope}:{name}"
            block_bounds = {}
            estimate = metric(scope, name, range(len(axis)))
            for label, values in distributions[key].items():
                tail = 1 - policy.confidence if rule.direction == "LOWER" else policy.confidence
                index = (
                    int(tail * (len(values) - 1))
                    if rule.direction == "LOWER"
                    else int((tail * (len(values) - 1)).to_integral_value(rounding="ROUND_CEILING"))
                )
                block_bounds[label] = (
                    sorted(values)[index] if values and not undefined[key][label] else None
                )
            bound = (
                (
                    min(value for value in block_bounds.values() if value is not None)
                    if rule.direction == "LOWER"
                    else max(value for value in block_bounds.values() if value is not None)
                )
                if block_bounds and all(value is not None for value in block_bounds.values())
                else None
            )
            if rule.direction == "EXACT":
                bound = estimate
            passed = (
                None
                if bound is None
                else (bound > rule.threshold if rule.strict else bound >= rule.threshold)
                if rule.direction in {"LOWER", "EXACT"}
                else (bound < rule.threshold if rule.strict else bound <= rule.threshold)
            )
            gates[key] = ExitGate(
                estimate=estimate,
                bound=bound,
                direction=rule.direction,
                threshold=rule.threshold,
                strict=rule.strict,
                applicable=(
                    any(
                        label.state == scopes[scope]
                        for position in positions
                        for label in position.decisions
                    )
                    if scope != "overall"
                    else bool(positions)
                )
                and (estimate is not None if name.startswith("overconfidence:") else True)
                and not name.endswith(":MIDDLE"),
                passed=passed,
                block_bounds=block_bounds,
                undefined_resamples=undefined[key],
            )
    watermarks = {scope: _watermark(positions, policy, state) for scope, state in scopes.items()}
    missing_evidence: set[str] = set()
    missing_by_scope: dict[str, set[str]] = {scope: set() for scope in scopes}
    gap_state: str | None = None

    def record_missing(reason: str) -> None:
        missing_evidence.add(reason)
        for scope, state in scopes.items():
            if state is None or gap_state is None or state == gap_state:
                missing_by_scope[scope].add(reason)

    calendar = synthetic_market_calendar(policy.market_calendar_version)
    assert calendar is not None
    for position in positions:
        gap_state = position.decisions[0].state if position.decisions else None
        if position.state == "UNAVAILABLE":
            record_missing(f"EXIT_PATH_MISSING:{position.lifecycle_id}")
        for decision in position.decisions:
            gap_state = decision.state
            if not decision.forecast.data_complete:
                continue
            forward = calendar.sessions_after_open(decision.cutoff_at, 60)
            for target_name in ("V60", *(f"D20:{grid}" for grid in policy.loss_grids)):
                horizon = 60 if target_name == "V60" else 20
                if len(forward) < horizon:
                    record_missing(
                        f"FORWARD_CALENDAR_MISSING:{position.lifecycle_id}:{decision.prediction_id}:{target_name}"
                    )
                    continue
                due_at = forward[horizon - 1].closed_at
                if due_at > cutoff:
                    continue
                if _target(decision, target_name) is None:
                    record_missing(
                        f"FORWARD_LABEL_MISSING:{position.lifecycle_id}:{decision.prediction_id}:{target_name}"
                    )
                for baseline in ("STATE_RATE", "TRANSPARENT"):
                    if target_name not in decision.forecast.baseline_probabilities.get(
                        baseline, {}
                    ):
                        record_missing(
                            f"CALIBRATION_BASELINE_MISSING:{position.lifecycle_id}:{decision.prediction_id}:{baseline}:{target_name}"
                        )
                if target_name not in decision.forecast.quantiles:
                    record_missing(
                        f"CONTINUOUS_FORECAST_MISSING:{position.lifecycle_id}:{decision.prediction_id}:{target_name}"
                    )
    due_gap = bool(missing_evidence)
    disposition = (
        "INDETERMINATE"
        if due_gap
        else "INSUFFICIENT"
        if not watermarks["overall"].sufficient
        else "INDETERMINATE"
        if any(not watermark.sufficient for watermark in watermarks.values())
        or any(gate.passed is None for gate in gates.values() if gate.applicable)
        else "PASSED"
        if all(gate.passed for gate in gates.values() if gate.applicable)
        else "FAILED"
    )
    diagnostics: dict[str, CalibrationDiagnostics] = {}
    pinball: dict[str, Decimal | None] = {}
    for scope, state in scopes.items():
        for target_name in ("V60", *(f"D20:{grid}" for grid in policy.loss_grids)):
            records = tuple(
                (label, _target(label, target_name))
                for position in positions
                for label in position.decisions
                if label.forecast.data_complete
                and (state is None or label.state == state)
                and _target(label, target_name) is not None
            )
            if records:
                observations = tuple(
                    CalibrationDiagnosticObservation(
                        raw_success_score=Decimal(0), terminal_success=target[1]
                    )
                    for _, target in records
                    if target
                )
                probabilities = tuple(target[0] for _, target in records if target)
                diagnostics[f"{scope}:{target_name}"] = summarize_probability_calibration(
                    observations, probabilities
                )
            errors = []
            for decision, target in records:
                estimate = decision.forecast.quantiles.get(target_name)
                if estimate is not None and target is not None:
                    error = target[2] - estimate
                    errors.append(
                        max(
                            policy.continuous_quantile * error,
                            (policy.continuous_quantile - 1) * error,
                        )
                    )
            pinball[f"{scope}:{target_name}"] = (
                sum(errors, Decimal(0)) / len(errors) if errors else None
            )
    watermarks = {
        scope: mark.model_copy(
            update={
                "disposition": "INDETERMINATE"
                if missing_by_scope[scope]
                else "INSUFFICIENT"
                if not mark.sufficient
                else "INDETERMINATE"
                if any(
                    gate.passed is None
                    for key, gate in gates.items()
                    if key.startswith(scope + ":") and gate.applicable
                )
                else "PASSED"
                if all(
                    gate.passed
                    for key, gate in gates.items()
                    if key.startswith(scope + ":") and gate.applicable
                )
                else "FAILED"
            }
        )
        for scope, mark in watermarks.items()
    }
    return ExitInference(
        disposition=disposition,
        watermarks=watermarks,
        gates=gates,
        diagnostics=diagnostics,
        pinball_loss=pinball,
        resampling=resampling,
        missing_evidence=tuple(sorted(missing_evidence)),
    )
