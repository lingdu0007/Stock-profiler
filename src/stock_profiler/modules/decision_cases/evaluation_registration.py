"""Freeze standard population identities with the source business commit."""

from datetime import datetime
from hashlib import sha256

from stock_profiler.modules.decision_cases.domain import ExternalResult, FrozenDecisionCase
from stock_profiler.modules.evaluation.contracts import EvaluationRegistration


def evaluation_identity(population: str, event_id: str, object_id: str) -> str:
    return (
        "standard-evaluation-" + sha256(f"{population}:{event_id}:{object_id}".encode()).hexdigest()
    )


def register_evaluation_members(
    case: FrozenDecisionCase, event_id: str, result: ExternalResult
) -> ExternalResult:
    registrations: list[EvaluationRegistration] = []
    if result.selection is not None:
        registrations.extend(
            EvaluationRegistration(
                evaluation_id=evaluation_identity("SELECTION", event_id, security),
                population="SELECTION",
                source_event_id=event_id,
                security_id=security,
                frozen_probability=None,
                registered_at=datetime.fromisoformat(case.knowledge_cutoff),
                admission_reason="FROZEN_SELECTION_MEMBER",
                market_calendar_version="synthetic-market-calendar-v1",
            )
            for security in result.selection.members
        )
    if result.candidate_release is not None:
        release = result.candidate_release
        for member in release.members:
            if member.calibrated_probability is None:
                continue
            for population in ("PROBABILITY", "CANDIDATE"):
                if population == "CANDIDATE" and not member.candidate:
                    continue
                registrations.append(
                    EvaluationRegistration(
                        evaluation_id=evaluation_identity(population, event_id, member.research_id),
                        population=population,
                        source_event_id=event_id,
                        security_id=member.security_id,
                        frozen_probability=member.calibrated_probability,
                        registered_at=release.knowledge_cutoff,
                        admission_reason="FROZEN_PROBABILITY"
                        if population == "PROBABILITY"
                        else "FROZEN_CANDIDATE",
                        market_calendar_version=release.market_calendar_version,
                    )
                )
    if not registrations:
        return result
    return result.model_copy(update={"evaluation_registrations": tuple(registrations)})
