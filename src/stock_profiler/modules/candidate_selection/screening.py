"""Replay a frozen additive screening artifact without fitting or selecting a model."""

from __future__ import annotations

import json
from calendar import monthrange
from datetime import datetime
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from hashlib import sha256
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, StrictBool

from stock_profiler.modules.candidate_selection.universe_evidence import EvidenceContract

Month = Annotated[str, Field(pattern=r"^[1-9][0-9]{3}-(0[1-9]|1[0-2])$")]


class SignalDefinition(EvidenceContract):
    signal_id: str
    semantics_version: str
    population: Literal["INDUSTRY", "UNIVERSE", "CONTEXT"]
    reverse: bool
    monotone: bool
    minimum_states: tuple[Literal["NON_POSITIVE_EQUITY", "INSUFFICIENT_HISTORY"], ...]


class InteractionDefinition(EvidenceContract):
    interaction_id: str
    left: str
    right: str


class ScreeningStrategy(EvidenceContract):
    version_id: str
    model_family: Literal["RESTRICTED_ADDITIVE_BINARY"]
    curve_basis: Literal["PIECEWISE_LINEAR"]
    positive_target: str
    terminal_target: str
    signals: tuple[SignalDefinition, ...]
    interactions: tuple[InteractionDefinition, ...]
    knots: tuple[Decimal, ...]
    maximum_interaction_rank: int = Field(gt=0, strict=True)
    selection_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_policy_version: str
    regularization_version: str
    minimum_mature_months: int = Field(gt=0, strict=True)
    rolling_mature_months: int = Field(gt=0, strict=True)
    training_weighting: Literal["MONTH_EQUAL_STOCK_EQUAL"]
    regularization_strength: Decimal = Field(ge=0, allow_inf_nan=False)
    training_start_month: Month
    label_horizon_months: int = Field(gt=0, strict=True)
    data_contracts: dict[str, str]
    label_contract_version: str = Field(min_length=1)
    entry_contract_version: str = Field(min_length=1)
    cost_contract_version: str = Field(min_length=1)
    evaluation_contract_version: str = Field(min_length=1)
    qualification_contract_version: str = Field(min_length=1)


class ScreeningTrainingMonth(EvidenceContract):
    month: Month
    selection_cutoff_at: AwareDatetime


class InteractionComponent(EvidenceContract):
    coefficient: Decimal
    left: tuple[Decimal, ...]
    right: tuple[Decimal, ...]


class ScreeningHead(EvidenceContract):
    intercept: Decimal
    main_effects: dict[str, tuple[Decimal, ...]]
    interactions: dict[str, tuple[InteractionComponent, ...]]


class ScreeningTrainingMember(EvidenceContract):
    month: Month
    security_id: str
    positive_label: StrictBool
    terminal_label: StrictBool
    label_available_at: AwareDatetime


class ScreeningArtifact(EvidenceContract):
    snapshot_id: str
    strategy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fitted_at: AwareDatetime
    label_available_through: AwareDatetime
    training_months: tuple[str, ...]
    training_calendar: tuple[ScreeningTrainingMonth, ...]
    training_members_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_members: tuple[ScreeningTrainingMember, ...]
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    diagnostics_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fit_diagnostics: dict[str, str]
    positive: ScreeningHead
    terminal: ScreeningHead


class SignalValue(EvidenceContract):
    signal_id: str
    raw: Decimal | None
    state: Literal["OBSERVED", "NON_POSITIVE_EQUITY", "INSUFFICIENT_HISTORY"]


class ScreeningObservation(EvidenceContract):
    security_id: str
    industry: str
    signals: tuple[SignalValue, ...]


class ScreeningSnapshot(EvidenceContract):
    strategy: ScreeningStrategy
    artifact: ScreeningArtifact
    observations: tuple[ScreeningObservation, ...]


class TransformedSignal(EvidenceContract):
    signal_id: str
    raw: Decimal | None
    state: str
    percentile: Decimal


class ScreeningContribution(EvidenceContract):
    security_id: str
    signals: tuple[TransformedSignal, ...]
    positive_contributions: dict[str, Decimal]
    terminal_contributions: dict[str, Decimal]
    positive_score: Decimal
    terminal_score: Decimal


def content_hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def replay_screening(
    snapshot: ScreeningSnapshot,
    *,
    cutoff: datetime,
    policy: object,
    strategy_version: str,
    snapshot_id: str,
    industries: dict[str, str | None],
    data_contracts: dict[str, str],
) -> tuple[tuple[ScreeningContribution, ...], tuple[str, ...]]:
    strategy, artifact = snapshot.strategy, snapshot.artifact
    strategy_payload = strategy.model_dump(mode="json", exclude={"version_id"})
    artifact_payload = artifact.model_dump(mode="json", exclude={"snapshot_id"})
    failures: list[str] = []
    if (
        strategy.version_id != strategy_version
        or strategy.version_id != f"sha256:{content_hash(strategy_payload)}"
        or artifact.snapshot_id != snapshot_id
        or artifact.snapshot_id != f"sha256:{content_hash(artifact_payload)}"
        or artifact.strategy_sha256 != content_hash(strategy.model_dump(mode="json"))
        or strategy.selection_policy_sha256 != content_hash(policy)
        or strategy.data_contracts != data_contracts
        or any(
            data_contracts.get(f"field:signal:{signal.signal_id}") != signal.semantics_version
            for signal in strategy.signals
        )
    ):
        failures.append("SCREENING_VERSION_MISMATCH")
    if (
        not artifact.label_available_through <= artifact.fitted_at <= cutoff
        or not artifact.training_months
        or tuple(sorted(set(artifact.training_months))) != artifact.training_months
        or not strategy.minimum_mature_months
        <= len(artifact.training_months)
        <= strategy.rolling_mature_months
        or {member.month for member in artifact.training_members} != set(artifact.training_months)
        or len({(member.month, member.security_id) for member in artifact.training_members})
        != len(artifact.training_members)
        or artifact.training_members_sha256
        != content_hash([member.model_dump(mode="json") for member in artifact.training_members])
        or any(
            member.label_available_at > artifact.label_available_through
            or (member.terminal_label and not member.positive_label)
            for member in artifact.training_members
        )
        or artifact.diagnostics_sha256 != content_hash(artifact.fit_diagnostics)
        or artifact.fit_diagnostics.get("status") != "CONVERGED"
        or not _training_window_valid(strategy, artifact, cutoff)
    ):
        failures.append("SCREENING_TRAINING_PROVENANCE_INVALID")
    signals = {signal.signal_id: signal for signal in strategy.signals}
    interactions = {term.interaction_id: term for term in strategy.interactions}
    if (
        not signals
        or len(signals) != len(strategy.signals)
        or len(interactions) != len(strategy.interactions)
        or set(signals) & set(interactions)
        or "intercept" in signals
        or "intercept" in interactions
        or any(
            term.left not in signals or term.right not in signals for term in interactions.values()
        )
    ):
        failures.append("SCREENING_FEATURE_CONTRACT_INVALID")
    knots = strategy.knots
    if (
        len(knots) < 2
        or any(not value.is_finite() for value in knots)
        or knots[0] != 0
        or knots[-1] != 100
        or any(left >= right for left, right in zip(knots, knots[1:], strict=False))
    ):
        failures.append("SCREENING_CURVE_CONTRACT_INVALID")
    main_signals = {
        key for key, definition in signals.items() if definition.population != "CONTEXT"
    }
    for head in (artifact.positive, artifact.terminal):
        if (
            not head.intercept.is_finite()
            or set(head.main_effects) != main_signals
            or set(head.interactions) != set(interactions)
        ):
            failures.append("SCREENING_HEAD_CONTRACT_INVALID")
        for key, curve in head.main_effects.items():
            if not _curve_valid(curve, len(knots)) or (
                key in signals
                and signals[key].monotone
                and any(left > right for left, right in zip(curve, curve[1:], strict=False))
            ):
                failures.append("SCREENING_CURVE_CONTRACT_INVALID")
        for components in head.interactions.values():
            if not 0 < len(components) <= strategy.maximum_interaction_rank:
                failures.append("SCREENING_INTERACTION_CONTRACT_INVALID")
            for component in components:
                if (
                    not component.coefficient.is_finite()
                    or not _curve_valid(component.left, len(knots))
                    or not _curve_valid(component.right, len(knots))
                ):
                    failures.append("SCREENING_INTERACTION_CONTRACT_INVALID")
    observations = {row.security_id: row for row in snapshot.observations}
    if len(observations) != len(snapshot.observations) or set(observations) != set(industries):
        failures.append("SCREENING_OBSERVATIONS_INCOMPLETE")
    for row in snapshot.observations:
        values = {value.signal_id: value for value in row.signals}
        if (
            row.industry != industries.get(row.security_id)
            or len(values) != len(row.signals)
            or set(values) != set(signals)
        ):
            failures.append("SCREENING_OBSERVATIONS_INCOMPLETE")
        for key, value in values.items():
            if key not in signals:
                continue
            if value.state == "OBSERVED":
                if value.raw is None or not value.raw.is_finite():
                    failures.append("SCREENING_SIGNAL_UNAVAILABLE")
                elif signals[key].population == "CONTEXT" and not 0 <= value.raw <= 100:
                    failures.append("SCREENING_CONTEXT_INVALID")
            elif value.state not in signals[key].minimum_states or value.raw is not None:
                failures.append("SCREENING_SIGNAL_STATE_INVALID")
    if failures:
        return (), tuple(dict.fromkeys(failures))
    values_by_key = {
        row.security_id: {value.signal_id: value for value in row.signals}
        for row in snapshot.observations
    }
    transformed: dict[str, dict[str, Fraction]] = {key: {} for key in observations}
    for key, definition in signals.items():
        context_values = {values[key].raw for values in values_by_key.values()}
        if definition.population == "CONTEXT" and len(context_values) > 1:
            return (), ("SCREENING_CONTEXT_INVALID",)
        for security_id, row in observations.items():
            value = values_by_key[security_id][key]
            if value.state != "OBSERVED":
                transformed[security_id][key] = Fraction(0)
                continue
            assert value.raw is not None
            if definition.population == "CONTEXT":
                transformed[security_id][key] = Fraction(value.raw)
                continue
            peers = [
                other[key]
                for other_id, other in values_by_key.items()
                if definition.population == "UNIVERSE"
                or observations[other_id].industry == row.industry
            ]
            favorable = value.raw.copy_negate() if definition.reverse else value.raw
            ranked = [
                peer.raw.copy_negate() if definition.reverse else peer.raw
                for peer in peers
                if peer.raw is not None
            ]
            less = sum(peer < favorable for peer in ranked) + sum(
                peer.raw is None for peer in peers
            )
            equal = sum(peer == favorable for peer in ranked)
            transformed[security_id][key] = (
                (less + Fraction(equal - 1, 2)) * 100 / max(1, len(peers) - 1)
            )
    audits = []
    with localcontext(Context(prec=38)):
        for security_id, row in observations.items():
            positive = _contributions(artifact.positive, strategy, transformed[security_id])
            terminal = _contributions(artifact.terminal, strategy, transformed[security_id])
            audits.append(
                ScreeningContribution(
                    security_id=security_id,
                    signals=tuple(
                        TransformedSignal(
                            signal_id=value.signal_id,
                            raw=value.raw,
                            state=value.state,
                            percentile=_decimal(transformed[security_id][value.signal_id]),
                        )
                        for value in row.signals
                    ),
                    positive_contributions={
                        key: _decimal(value) for key, value in positive.items()
                    },
                    terminal_contributions={
                        key: _decimal(value) for key, value in terminal.items()
                    },
                    positive_score=_decimal(sum(positive.values(), Fraction(0))),
                    terminal_score=_decimal(sum(terminal.values(), Fraction(0))),
                )
            )
    return tuple(audits), ()


def _training_window_valid(
    strategy: ScreeningStrategy,
    artifact: ScreeningArtifact,
    cutoff: datetime,
) -> bool:
    start = datetime.strptime(strategy.training_start_month, "%Y-%m")
    local_cutoff = cutoff.astimezone(ZoneInfo("Asia/Shanghai"))
    first = start.year * 12 + start.month - 1
    last = local_cutoff.year * 12 + local_cutoff.month - 1 - strategy.label_horizon_months
    expected_calendar = tuple(
        f"{year:04}-{month + 1:02}"
        for year, month in (divmod(index, 12) for index in range(first, last + 1))
    )
    if tuple(row.month for row in artifact.training_calendar) != expected_calendar:
        return False
    mature: dict[str, datetime] = {}
    for row in artifact.training_calendar:
        selected = row.selection_cutoff_at.astimezone(ZoneInfo("Asia/Shanghai"))
        if selected.strftime("%Y-%m") != row.month:
            return False
        year, month_zero = divmod(
            selected.year * 12 + selected.month - 1 + strategy.label_horizon_months,
            12,
        )
        maturity = selected.replace(
            year=year,
            month=month_zero + 1,
            day=min(selected.day, monthrange(year, month_zero + 1)[1]),
        )
        if maturity <= cutoff:
            mature[row.month] = maturity
    expected_window = tuple(mature)[-strategy.rolling_mature_months :]
    return artifact.training_months == expected_window and all(
        member.month in mature and mature[member.month] <= member.label_available_at
        for member in artifact.training_members
    )


def _curve_valid(curve: tuple[Decimal, ...], size: int) -> bool:
    return len(curve) == size and all(value.is_finite() for value in curve)


def _curve_value(knots: tuple[Decimal, ...], curve: tuple[Decimal, ...], x: Fraction) -> Fraction:
    index = sum(x > knot for knot in knots[1:-1])
    left, right = Fraction(knots[index]), Fraction(knots[index + 1])
    weight = (x - left) / (right - left)
    return Fraction(curve[index]) * (1 - weight) + Fraction(curve[index + 1]) * weight


def _contributions(
    head: ScreeningHead, strategy: ScreeningStrategy, values: dict[str, Fraction]
) -> dict[str, Fraction]:
    result = {"intercept": Fraction(head.intercept)}
    for key, curve in head.main_effects.items():
        result[key] = _curve_value(strategy.knots, curve, values[key])
    for term in strategy.interactions:
        result[term.interaction_id] = sum(
            (
                Fraction(component.coefficient)
                * _curve_value(strategy.knots, component.left, values[term.left])
                * _curve_value(strategy.knots, component.right, values[term.right])
                for component in head.interactions[term.interaction_id]
            ),
            Fraction(0),
        )
    return result


def _decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)
