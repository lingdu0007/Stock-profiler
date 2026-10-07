"""Cumulative immutable evidence and authoritative correction rules for evaluation."""

from datetime import datetime

from stock_profiler.modules.evaluation.contracts import OutcomeEvidence, StandardObservation
from stock_profiler.modules.evaluation.historical_contracts import (
    HistoricalMonthInput,
    HistoricalSelectionCommand,
    WealthPath,
)
from stock_profiler.modules.evaluation.service import validate_evidence_revision


def merge_history(
    history: tuple[HistoricalSelectionCommand, ...], current: HistoricalSelectionCommand
) -> dict[str, HistoricalMonthInput]:
    return merge_month_evidence(
        tuple((command.cutoff_at, command.months) for command in (*history, current))
    )


def merge_month_evidence(
    revisions: tuple[tuple[datetime, tuple[HistoricalMonthInput, ...]], ...],
) -> dict[str, HistoricalMonthInput]:
    retained: dict[str, HistoricalMonthInput] = {}
    evidence_by_id: dict[str, OutcomeEvidence] = {}
    for cutoff, months in revisions:
        for incoming in months:
            previous = retained.get(incoming.plan_month)
            if previous is not None and incoming.selection_event_id != previous.selection_event_id:
                raise ValueError("HISTORICAL_SELECTION_IDENTITY_CHANGED")
            if previous is not None and any(
                new is not None and old is not None and new != old
                for new, old in (
                    (incoming.index, previous.index),
                    (incoming.factors, previous.factors),
                )
            ):
                raise ValueError("HISTORICAL_SELECTION_CONTEXT_CHANGED")
            observations = (
                {row.security_id: row for row in previous.observations} if previous else {}
            )
            if len({row.security_id for row in incoming.observations}) != len(
                incoming.observations
            ):
                raise ValueError("HISTORICAL_OUTCOME_MEMBERSHIP_DUPLICATED")
            for row in incoming.observations:
                prior = observations.get(row.security_id)
                for new, old in (
                    (row.entry, prior.entry if prior else None),
                    (row.terminal, prior.terminal if prior else None),
                ):
                    if new is not None:
                        _validate(new, old, evidence_by_id, cutoff)
                observations[row.security_id] = StandardObservation(
                    security_id=row.security_id,
                    entry=row.entry or (prior.entry if prior else None),
                    terminal=row.terminal or (prior.terminal if prior else None),
                )
            paths = {row.security_id: row for row in previous.paths} if previous else {}
            if len({row.security_id for row in incoming.paths}) != len(incoming.paths):
                raise ValueError("HISTORICAL_PATH_MEMBERSHIP_DUPLICATED")
            for path in incoming.paths:
                marks = (
                    {mark.effective_at: mark for mark in paths[path.security_id].marks}
                    if path.security_id in paths
                    else {}
                )
                if len({mark.effective_at for mark in path.marks}) != len(path.marks):
                    raise ValueError("HISTORICAL_PATH_SESSION_DUPLICATED")
                for mark in path.marks:
                    _validate(mark, marks.get(mark.effective_at), evidence_by_id, cutoff)
                    marks[mark.effective_at] = mark
                paths[path.security_id] = WealthPath(
                    security_id=path.security_id, marks=tuple(marks[at] for at in sorted(marks))
                )
            for context in (incoming.index, incoming.factors, incoming.future_index):
                if context is not None:
                    previous_context = (
                        previous.future_index
                        if previous and context is incoming.future_index
                        else evidence_by_id.get(context.evidence_id)
                    )
                    _validate(context, previous_context, evidence_by_id, cutoff)
            retained[incoming.plan_month] = incoming.model_copy(
                update={
                    "observations": tuple(observations.values()),
                    "paths": tuple(paths.values()),
                    "standard_event_id": incoming.standard_event_id
                    or (previous.standard_event_id if previous else None),
                    "index": incoming.index or (previous.index if previous else None),
                    "factors": incoming.factors or (previous.factors if previous else None),
                    "future_index": incoming.future_index
                    or (previous.future_index if previous else None),
                }
            )
    return retained


def _validate(
    new: OutcomeEvidence,
    old: OutcomeEvidence | None,
    by_id: dict[str, OutcomeEvidence],
    cutoff: datetime,
) -> None:
    if new.evidence_id in by_id and by_id[new.evidence_id] != new:
        raise ValueError("HISTORICAL_EVIDENCE_IDENTITY_REDEFINED")
    try:
        validate_evidence_revision(new, old, by_id, cutoff)
    except ValueError as error:
        raise ValueError(str(error).replace("STANDARD_", "HISTORICAL_")) from error
    by_id[new.evidence_id] = new
