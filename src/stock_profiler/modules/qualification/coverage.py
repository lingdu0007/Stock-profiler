"""Explicit coverage status for the absence of deterministic policy modules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeterministicPolicyCoverage:
    """Reports inapplicability rather than pretending this baseline has policy."""

    status: str
    branch_coverage_target: int
    modules: tuple[str, ...]


def deterministic_policy_coverage() -> DeterministicPolicyCoverage:
    """State the scoped baseline truth without introducing a policy engine."""
    return DeterministicPolicyCoverage(
        status="not-applicable",
        branch_coverage_target=100,
        modules=(),
    )
