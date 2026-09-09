"""Immutable source evidence retained with a monthly universe."""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class EvidenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseEvidence(EvidenceContract):
    evidence_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    authority: Literal["EXCHANGE", "BROKER", "CERTIFIED_DELIVERY", "EXPLORATORY"]
    license_id: str = Field(min_length=1)
    license_valid_from: AwareDatetime
    license_valid_until: AwareDatetime
    licensed_purposes: tuple[
        Literal["SYNTHETIC", "HISTORICAL_RECONSTRUCTED", "REAL_CANDIDATE"], ...
    ]
    retention_permitted: bool
    complete: bool
    conflict: bool
    fact_effective_at: AwareDatetime
    source_published_at: AwareDatetime | None
    source_observed_at: AwareDatetime | None
    acquired_at: AwareDatetime | None
    validated_at: AwareDatetime | None
    complete_through: AwareDatetime
    content: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def failures(self, cutoff: datetime, expected: object, purpose: str) -> tuple[str, ...]:
        reasons: list[str] = []
        clocks = (
            self.source_published_at,
            self.source_observed_at,
            self.acquired_at,
            self.validated_at,
        )
        if any(instant is None or instant > cutoff for instant in clocks):
            reasons.append("EVIDENCE_NOT_AVAILABLE")
        if (
            self.acquired_at is not None
            and self.validated_at is not None
            and self.validated_at < self.acquired_at
        ) or (
            self.source_observed_at is not None
            and self.acquired_at is not None
            and self.acquired_at < self.source_observed_at
        ):
            reasons.append("EVIDENCE_CLOCK_ORDER")
        if self.fact_effective_at > cutoff:
            reasons.append("FUTURE_FACT")
        if not self.complete or self.complete_through < cutoff:
            reasons.append("SEMANTIC_COMPLETENESS_FAILED")
        if not self.retention_permitted:
            reasons.append("EVIDENCE_RETENTION_UNLICENSED")
        if (
            not self.license_valid_from <= cutoff <= self.license_valid_until
            or purpose not in self.licensed_purposes
        ):
            reasons.append("EVIDENCE_USE_UNLICENSED")
        if self.conflict:
            reasons.append("SOURCE_CONFLICT")
        if sha256(self.content.encode()).hexdigest() != self.content_sha256:
            reasons.append("CONTENT_INTEGRITY_FAILED")
        try:
            original = json.loads(self.content)
        except (ValueError, TypeError):
            reasons.append("CONTENT_INVALID")
        else:
            if original != expected:
                reasons.append("SOURCE_FACT_MISMATCH")
        return tuple(reasons)


class UniverseSourceSubstitution(EvidenceContract):
    certification_id: str = Field(min_length=1)
    registered_at: AwareDatetime
    valid_until: AwareDatetime
    primary_source: str
    alternate_source: str
    alternate_version: str
    field_family: str
    manifest_version: str
    semantics_version: str
    purpose: str
    reason: Literal["PRIMARY_UNAVAILABLE", "PRIMARY_CONFLICT"]
    checks: tuple[Literal["SEMANTICS", "LICENSE", "COMPLETENESS", "REPLAY", "SHADOW"], ...]
    authority_evidence: UniverseEvidence


class UniverseManifestEntry(EvidenceContract):
    field_family: str = Field(min_length=1)
    requirement: Literal["REQUIRED", "EXPLORATORY"]
    semantics_version: str = Field(min_length=1)
    primary_source: str = Field(min_length=1)
    evidence: UniverseEvidence | None
    substitution: UniverseSourceSubstitution | None = None

    def failures(
        self,
        *,
        cutoff: datetime,
        expected: object,
        authority: str,
        manifest_version: str,
        purpose: str,
    ) -> tuple[str, ...]:
        evidence = self.evidence
        if evidence is None:
            return ("REQUIRED_EVIDENCE_MISSING",)
        reasons = list(evidence.failures(cutoff, expected, purpose))
        if evidence.source == self.primary_source:
            if evidence.authority != authority or self.substitution is not None:
                reasons.append("FACT_AUTHORITY_MISMATCH")
            return tuple(reasons)
        substitution = self.substitution
        if (
            substitution is None
            or substitution.primary_source != self.primary_source
            or substitution.alternate_source != evidence.source
            or substitution.alternate_version != evidence.source_version
            or substitution.field_family != self.field_family
            or substitution.manifest_version != manifest_version
            or substitution.semantics_version != self.semantics_version
            or substitution.purpose != purpose
            or evidence.acquired_at is None
            or not substitution.registered_at < evidence.acquired_at <= cutoff
            or substitution.valid_until < cutoff
            or set(substitution.checks)
            != {"SEMANTICS", "LICENSE", "COMPLETENESS", "REPLAY", "SHADOW"}
            or evidence.authority != "CERTIFIED_DELIVERY"
        ):
            return (*reasons, "UNQUALIFIED_SUBSTITUTION")
        authority_evidence = substitution.authority_evidence
        if authority_evidence.authority != authority:
            reasons.append("FACT_AUTHORITY_MISMATCH")
        reasons.extend(authority_evidence.failures(cutoff, expected, purpose))
        return tuple(reasons)


class UniverseDataManifest(EvidenceContract):
    version_id: str = Field(min_length=1)
    entries: tuple[UniverseManifestEntry, ...]
