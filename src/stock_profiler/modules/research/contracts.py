"""Frozen research, raw-score, risk-veto, and handoff contracts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from hashlib import sha256
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

RESEARCH_CONTRACT_VERSION = "1.0.0"
RESEARCH_DEFINITION_ID = "synthetic-monthly-research"
RESEARCH_DEFINITION_VERSION = "2.0.0"
RESEARCH_MODEL_ADAPTER_ID = "m-agent-deterministic-research-adapter"
RESEARCH_ROUTING_POLICY_VERSION = "monthly-research-risk-veto-v1"
RESEARCH_OUTPUT_CONTRACT_ID = "synthetic-monthly-research-draft"
RESEARCH_OUTPUT_CONTRACT_VERSION = "1.0.0"
RISK_DEFINITION_ID = "synthetic-independent-risk-veto"
RISK_DEFINITION_VERSION = "1.0.0"
RISK_MODEL_ADAPTER_ID = "m-agent-deterministic-risk-adapter"
RISK_OUTPUT_CONTRACT_ID = "synthetic-independent-risk-veto"
RISK_OUTPUT_CONTRACT_VERSION = "1.0.0"
RESEARCH_SCOPE: Literal["D0_SYNTHETIC_RESEARCH_ONLY"] = "D0_SYNTHETIC_RESEARCH_ONLY"
RESEARCH_ANNOUNCEMENT_TOOL_VERSION = "synthetic-announcement-tool-v1"
ResearchDataType = Literal[
    "DAILY_MARKET",
    "MONEY_FLOW",
    "INSTITUTIONAL_ACTIVITY",
    "FINANCIAL_STATEMENTS",
]
RESEARCH_REQUIRED_DATA_TYPES: tuple[ResearchDataType, ...] = (
    "DAILY_MARKET",
    "MONEY_FLOW",
    "INSTITUTIONAL_ACTIVITY",
    "FINANCIAL_STATEMENTS",
)

RAW_SCORE_MODEL_VERSION = "elastic-net-logistic-z20-v1"
RAW_SCORE_TARGET: Literal["SIX_MONTH_TERMINAL_20_PERCENT"] = "SIX_MONTH_TERMINAL_20_PERCENT"
RAW_SCORE_INTERCEPT = Decimal("-0.40")
RAW_SCORE_L1_RATIO = Decimal("0.25")
RAW_SCORE_L2_RATIO = Decimal("0.75")
RAW_SCORE_INTERACTION_TERMS: tuple[str, ...] = ()
RAW_SCORE_FEATURE_IDS: tuple[str, ...] = (
    "single_quarter_revenue_acceleration",
    "asset_normalized_quarter_profit_improvement",
    "operating_cash_flow_return_on_assets",
    "working_capital_pressure_change",
    "leverage_ratio_change",
    "industry_relative_return_20d",
    "downside_semivariance_60d",
    "max_drawdown_60d",
    "turnover_change",
    "institutional_net_buy_ratio",
    "institutional_listing_frequency",
)
RAW_SCORE_COEFFICIENTS: dict[str, Decimal] = {
    "screening_positive_prior": Decimal("0.15"),
    "screening_terminal_prior": Decimal("0.35"),
    "single_quarter_revenue_acceleration": Decimal("0.08"),
    "asset_normalized_quarter_profit_improvement": Decimal("0.07"),
    "operating_cash_flow_return_on_assets": Decimal("0.06"),
    "working_capital_pressure_change": Decimal("-0.05"),
    "leverage_ratio_change": Decimal("-0.04"),
    "industry_relative_return_20d": Decimal("0.09"),
    "downside_semivariance_60d": Decimal("-0.03"),
    "max_drawdown_60d": Decimal("-0.04"),
    "turnover_change": Decimal("0.02"),
    "institutional_net_buy_ratio": Decimal("0.05"),
    "institutional_listing_frequency": Decimal("0.06"),
}


class RawScoreCalculationError(ValueError):
    """A structured raw-score calculation failed without a downstream result."""


class ResearchContract(BaseModel):
    """Reject unversioned research fields and mutable contract payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchEvidence(ResearchContract):
    evidence_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    acquired_at: AwareDatetime
    validated_at: AwareDatetime
    knowledge_cutoff: AwareDatetime
    semantic_version: str = Field(min_length=1)
    validation_status: Literal["VALIDATED"]


class ResearchToolEvidence(ResearchContract):
    """Structured provenance emitted by the allowlisted exploratory Tool."""

    evidence_id: str = Field(pattern=r"^announcement:.+")
    source: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    acquired_at: AwareDatetime
    validated_at: AwareDatetime
    knowledge_cutoff: AwareDatetime
    semantic_version: str = Field(min_length=1)
    validation_status: Literal["VALIDATED"]


class FrozenDualTargetScreening(ResearchContract):
    """The full-domain, two-head screening output handed to fixed-ten research."""

    positive_target: Literal["SIX_MONTH_POSITIVE_RETURN"]
    terminal_target: Literal["SIX_MONTH_TERMINAL_20_PERCENT"]
    strategy_version: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    universe_security_ids: tuple[str, ...] = Field(min_length=10)
    selected_member_ids: tuple[str, ...] = Field(min_length=10, max_length=10)
    positive_scores: dict[str, Decimal]
    terminal_scores: dict[str, Decimal]
    positive_percentiles: dict[str, Decimal]
    terminal_percentiles: dict[str, Decimal]
    positive_head_version: str = Field(min_length=1)
    terminal_head_version: str = Field(min_length=1)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_dual_head_output(self) -> FrozenDualTargetScreening:
        universe_ids = set(self.universe_security_ids)
        selected_ids = set(self.selected_member_ids)
        if len(universe_ids) != len(self.universe_security_ids):
            raise ValueError("screening universe security identities must be unique")
        if len(selected_ids) != 10:
            raise ValueError("screening output must contain exactly ten selected members")
        if not selected_ids.issubset(universe_ids):
            raise ValueError("selected screening members must belong to the full domain")
        if (
            set(self.positive_scores) != universe_ids
            or set(self.terminal_scores) != universe_ids
            or set(self.positive_percentiles) != universe_ids
            or set(self.terminal_percentiles) != universe_ids
        ):
            raise ValueError("screening targets and percentiles must cover the full domain")
        if any(
            not value.is_finite()
            for value in (
                *self.positive_scores.values(),
                *self.terminal_scores.values(),
                *self.positive_percentiles.values(),
                *self.terminal_percentiles.values(),
            )
        ):
            raise ValueError("screening scores and percentiles must be finite")
        if any(
            value < 0 or value > 100
            for value in (*self.positive_percentiles.values(), *self.terminal_percentiles.values())
        ):
            raise ValueError("screening percentiles must be between zero and one hundred")
        if self.output_sha256 != screening_output_sha256(self):
            raise ValueError("screening output hash does not match canonical output")
        return self


class ResearchDataManifestEntry(ResearchContract):
    """One complete, cutoff-bound Provider contract for a member."""

    data_type: ResearchDataType
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    completeness: Literal["COMPLETE", "INCOMPLETE"]
    event_status: Literal["PRESENT", "VERIFIED_EMPTY"]
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    knowledge_cutoff: AwareDatetime

    @model_validator(mode="after")
    def validate_availability(self) -> ResearchDataManifestEntry:
        if self.completeness != "COMPLETE":
            raise ValueError("research data manifest entries must be complete")
        if self.event_status == "VERIFIED_EMPTY" and self.data_type != "INSTITUTIONAL_ACTIVITY":
            raise ValueError("only institutional activity may use a verified-empty event status")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("research data manifest evidence identities must be unique")
        return self


class ResearchDataManifest(ResearchContract):
    """The per-stock required-data availability contract delivered by a Provider."""

    version: str = Field(min_length=1)
    entries: tuple[ResearchDataManifestEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_required_data_types(self) -> ResearchDataManifest:
        actual = tuple(entry.data_type for entry in self.entries)
        if actual != RESEARCH_REQUIRED_DATA_TYPES:
            raise ValueError(
                "research data manifest must contain the required research data types exactly once"
            )
        return self


class ResearchMemberInput(ResearchContract):
    """Structured, cutoff-bound facts for one member of the research cohort."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    knowledge_cutoff: AwareDatetime
    evidence: tuple[ResearchEvidence, ...] = Field(min_length=1)
    data_manifest: ResearchDataManifest
    structured_signals: dict[str, Decimal]
    risk_flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_member_facts(self) -> ResearchMemberInput:
        if set(self.structured_signals) != set(RAW_SCORE_FEATURE_IDS):
            raise ValueError("structured research facts must contain all eleven raw-score signals")
        if any(not value.is_finite() for value in self.structured_signals.values()):
            raise ValueError("structured research signals must be finite")
        evidence_ids = [evidence.evidence_id for evidence in self.evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("research evidence identities must be unique per member")
        manifest_evidence_ids = tuple(
            evidence_id
            for entry in self.data_manifest.entries
            for evidence_id in entry.evidence_ids
        )
        if set(manifest_evidence_ids) != set(evidence_ids):
            raise ValueError(
                "research data manifest must account for every Provider evidence identity"
            )
        if len(set(manifest_evidence_ids)) != len(manifest_evidence_ids):
            raise ValueError("research data manifest evidence identities must be unique")
        if any(
            entry.knowledge_cutoff != self.knowledge_cutoff for entry in self.data_manifest.entries
        ):
            raise ValueError("research data manifest and member knowledge cutoffs must agree")
        if any(evidence.knowledge_cutoff != self.knowledge_cutoff for evidence in self.evidence):
            raise ValueError("research evidence and member knowledge cutoffs must agree")
        if any(
            evidence.acquired_at > evidence.validated_at
            or evidence.validated_at > self.knowledge_cutoff
            or evidence.validation_status != "VALIDATED"
            for evidence in self.evidence
        ):
            raise ValueError("research evidence must be validated and available by the cutoff")
        return self


class ResearchMemberHandoff(ResearchContract):
    """Immutable per-member evidence and risk facts consumed by risk."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    evidence: tuple[ResearchEvidence, ...] = Field(min_length=1)
    risk_flags: tuple[str, ...] = ()


class ResearchCommand(ResearchContract):
    """The frozen host input for one fixed-ten monthly research run."""

    contract_version: Literal["1.0.0"]
    synthetic: Literal[True]
    generator_version: str = Field(min_length=1)
    seed: int
    selection_object_id: str = Field(min_length=1)
    selection_event_id: str = Field(min_length=1)
    selection_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cutoff_at: AwareDatetime
    knowledge_cutoff: AwareDatetime
    purpose: Literal["SYNTHETIC"]
    screening: FrozenDualTargetScreening
    members: tuple[ResearchMemberInput, ...]
    failure_mode: Literal["NONE", "DATA", "RESEARCH", "RAW_SCORE", "RISK", "SYSTEM"] = "NONE"
    risk_scenario: Literal["ACCEPT", "REJECT"] = "ACCEPT"

    @model_validator(mode="after")
    def validate_fixed_ten_contract(self) -> ResearchCommand:
        if len(self.members) != 10:
            raise ValueError("research cohort must contain exactly ten members")
        security_ids = [member.security_id for member in self.members]
        research_ids = [member.research_id for member in self.members]
        if len(set(security_ids)) != 10:
            raise ValueError("research cohort security identities must be unique")
        if len(set(research_ids)) != 10:
            raise ValueError("research identities must be independent and unique")
        if self.cutoff_at != self.knowledge_cutoff:
            raise ValueError("research cutoff and knowledge cutoff must agree")
        if self.selection_fingerprint != selection_binding_sha256(
            self.selection_object_id,
            self.selection_event_id,
            self.cutoff_at,
            self.screening,
        ):
            raise ValueError("research selection binding does not match its frozen screening")
        if tuple(security_ids) != tuple(self.screening.selected_member_ids):
            raise ValueError("research members must preserve the frozen selected cohort order")
        if any(member.knowledge_cutoff != self.knowledge_cutoff for member in self.members):
            raise ValueError("all research members must share the frozen knowledge cutoff")
        evidence_ids = [
            evidence.evidence_id for member in self.members for evidence in member.evidence
        ]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("research evidence identities must be globally unique")
        return self


class ResearchDraftMember(ResearchContract):
    """The only per-stock content the research model may produce."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    thesis: str = Field(min_length=1)
    bull_case: str = Field(min_length=1)
    bear_case: str = Field(min_length=1)
    knowledge_cutoff: AwareDatetime


class ResearchDraft(ResearchContract):
    """Typed research text; it deliberately has no score, probability, or action."""

    contract_version: Literal["1.0.0"]
    members: tuple[ResearchDraftMember, ...] = Field(min_length=10, max_length=10)

    @model_validator(mode="after")
    def validate_draft_members(self) -> ResearchDraft:
        if len({member.security_id for member in self.members}) != 10:
            raise ValueError("research draft must contain ten unique securities")
        if len({member.research_id for member in self.members}) != 10:
            raise ValueError("research draft must contain ten unique research identities")
        return self


class RiskGate(ResearchContract):
    gate_id: str = Field(min_length=1)
    status: Literal["PASSED", "FAILED"]


class RiskMemberVeto(ResearchContract):
    """Independent risk disposition and gates for one fixed cohort member."""

    security_id: str = Field(min_length=1)
    research_id: str = Field(min_length=1)
    disposition: Literal["ACCEPTED", "REJECTED"]
    gates: tuple[RiskGate, ...] = Field(min_length=1)
    reasons: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_disposition_against_gates(self) -> RiskMemberVeto:
        expected = "REJECTED" if any(gate.status == "FAILED" for gate in self.gates) else "ACCEPTED"
        if self.disposition != expected:
            raise ValueError("risk member disposition must match its gates")
        return self


class RiskVetoDraft(ResearchContract):
    """Typed output of the independent risk Definition and Run."""

    contract_version: Literal["1.0.0"]
    handoff_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["ACCEPTED", "REJECTED"]
    gates: tuple[RiskGate, ...] = Field(min_length=1)
    reasons: tuple[str, ...] = Field(min_length=1)
    member_vetoes: tuple[RiskMemberVeto, ...] = ()

    @model_validator(mode="after")
    def validate_member_vetoes(self) -> RiskVetoDraft:
        if self.member_vetoes:
            member_ids = [member.security_id for member in self.member_vetoes]
            research_ids = [member.research_id for member in self.member_vetoes]
            if len(member_ids) != 10 or len(set(member_ids)) != 10:
                raise ValueError("risk veto must contain one result for each fixed-ten member")
            if len(set(research_ids)) != 10:
                raise ValueError("risk veto research identities must be independent and unique")
            member_dispositions = {member.disposition for member in self.member_vetoes}
            expected = "REJECTED" if "REJECTED" in member_dispositions else "ACCEPTED"
            if self.disposition != expected:
                raise ValueError("cohort risk disposition must summarize member vetoes")
        return self


class RawScore(ResearchContract):
    """The structured, uncalibrated terminal-target score frozen after the cohort."""

    security_id: str
    research_id: str
    target: Literal["SIX_MONTH_TERMINAL_20_PERCENT"]
    model_version: str
    intercept: Decimal
    structured_inputs: dict[str, Decimal]
    coefficients: dict[str, Decimal]
    contributions: dict[str, Decimal]
    z20: Decimal
    probability: None = None
    interaction_terms: tuple[str, ...] = ()
    l1_ratio: Decimal
    l2_ratio: Decimal


class ResearchFrameworkOutput(ResearchContract):
    """Adapter envelope joining two durable Runs without adding a business score."""

    research_run_id: str = Field(min_length=1)
    risk_run_id: str | None = None
    draft: ResearchDraft
    risk_veto: RiskVetoDraft | None = None
    raw_scores: tuple[RawScore, ...] | None = None
    tool_evidence_refs: tuple[str, ...] = ()
    tool_evidence: tuple[ResearchToolEvidence, ...] = ()
    member_handoffs: tuple[ResearchMemberHandoff, ...] = ()


@dataclass(frozen=True)
class ResearchRiskPlan:
    """Pure, immutable input plan for the independent risk Definition."""

    raw_scores: tuple[RawScore, ...]
    tool_evidence_refs: tuple[str, ...]
    tool_evidence: tuple[ResearchToolEvidence, ...]
    member_handoffs: tuple[ResearchMemberHandoff, ...]
    risk_run_id: str
    handoff_fingerprint: str
    input_payload: str


class RiskVetoOutcome(ResearchContract):
    run_id: str
    definition_id: str
    definition_version: str
    disposition: Literal["ACCEPTED", "REJECTED"]
    gates: tuple[RiskGate, ...]
    reasons: tuple[str, ...]
    member_vetoes: tuple[RiskMemberVeto, ...] = ()


class ResearchHandoff(ResearchContract):
    """Versioned, immutable boundary between research/risk and later work."""

    contract_version: Literal["1.0.0"]
    scope: Literal["D0_SYNTHETIC_RESEARCH_ONLY"]
    selection_object_id: str
    selection_event_id: str
    selection_fingerprint: str
    security_ids: tuple[str, ...] = Field(min_length=10, max_length=10)
    targets: tuple[str, ...] = Field(min_length=2, max_length=2)
    cutoff_at: AwareDatetime
    knowledge_cutoff: AwareDatetime
    evidence_ids: tuple[str, ...] = Field(min_length=10)
    research_run_id: str
    risk_run_id: str
    research_definition_id: str
    research_definition_version: str
    risk_definition_id: str
    risk_definition_version: str
    research_model_adapter_id: str
    risk_model_adapter_id: str
    research_routing_policy_version: str
    research_output_contract_id: str
    research_output_contract_version: str
    risk_output_contract_id: str
    risk_output_contract_version: str
    screening_strategy_version: str
    screening_snapshot_id: str
    raw_scores: tuple[RawScore, ...] = Field(min_length=10, max_length=10)
    tool_evidence: tuple[ResearchToolEvidence, ...] = ()
    member_handoffs: tuple[ResearchMemberHandoff, ...] = ()
    risk_veto: RiskVetoOutcome | None = None
    actionable: Literal[False] = False

    @model_validator(mode="after")
    def validate_targets(self) -> ResearchHandoff:
        if self.targets != (
            "SIX_MONTH_POSITIVE_RETURN",
            "SIX_MONTH_TERMINAL_20_PERCENT",
        ):
            raise ValueError("research handoff must bind the two frozen screening targets")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("research handoff evidence identities must be unique")
        return self


class ResearchMemberResult(ResearchContract):
    security_id: str
    research_id: str
    evidence_refs: tuple[str, ...]
    thesis: str
    bull_case: str
    bear_case: str
    knowledge_cutoff: AwareDatetime


class ResearchOutcome(ResearchContract):
    """Host-owned research result, including independent risk disposition."""

    disposition: Literal["FROZEN", "REJECTED", "DATA_FAILED", "SYSTEM_FAILED", "BLOCKED"]
    members: tuple[ResearchMemberResult, ...]
    raw_scores: tuple[RawScore, ...] | None = None
    risk_veto: RiskVetoOutcome | None = None
    tool_evidence: tuple[ResearchToolEvidence, ...] = ()
    handoff: ResearchHandoff
    reasons: tuple[str, ...] = Field(min_length=1)
    actionable: Literal[False] = False


def freeze_raw_score(command: ResearchCommand, member: ResearchMemberInput) -> RawScore:
    """Compute z20 only from structured screening priors and eleven signals."""
    if member.security_id not in command.screening.selected_member_ids:
        raise ValueError("raw score member is outside the fixed-ten cohort")
    structured_inputs = {
        "screening_positive_prior": command.screening.positive_percentiles[member.security_id],
        "screening_terminal_prior": command.screening.terminal_percentiles[member.security_id],
        **member.structured_signals,
    }
    try:
        with localcontext(Context(prec=38)):
            contributions = {
                key: structured_inputs[key] * RAW_SCORE_COEFFICIENTS[key]
                for key in structured_inputs
            }
            z20 = RAW_SCORE_INTERCEPT + sum(contributions.values(), Decimal("0"))
            if not z20.is_finite() or any(
                not value.is_finite() for value in contributions.values()
            ):
                raise ArithmeticError("raw score is not finite")
    except ArithmeticError as error:
        raise RawScoreCalculationError("RAW_SCORE_CALCULATION_FAILED") from error
    return RawScore(
        security_id=member.security_id,
        research_id=member.research_id,
        target=RAW_SCORE_TARGET,
        model_version=RAW_SCORE_MODEL_VERSION,
        intercept=RAW_SCORE_INTERCEPT,
        structured_inputs=structured_inputs,
        coefficients=dict(RAW_SCORE_COEFFICIENTS),
        contributions=contributions,
        z20=z20,
        interaction_terms=RAW_SCORE_INTERACTION_TERMS,
        l1_ratio=RAW_SCORE_L1_RATIO,
        l2_ratio=RAW_SCORE_L2_RATIO,
    )


def screening_output_sha256(screening: FrozenDualTargetScreening) -> str:
    """Hash the canonical dual-head output without its self-referential digest."""
    payload = screening.model_dump(mode="json", exclude={"output_sha256"})
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def selection_binding_sha256(
    selection_object_id: str,
    selection_event_id: str,
    cutoff_at: AwareDatetime,
    screening: FrozenDualTargetScreening,
) -> str:
    """Bind the upstream selection identity to its ordered screening artifact."""
    payload = {
        "selection_object_id": selection_object_id,
        "selection_event_id": selection_event_id,
        "cutoff_at": cutoff_at.isoformat(),
        "screening": screening.model_dump(mode="json"),
    }
    return sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def handoff_fingerprint(
    command: ResearchCommand,
    draft: ResearchDraft,
    *,
    raw_scores: tuple[RawScore, ...] | None = None,
    tool_evidence_refs: tuple[str, ...] = (),
    tool_evidence: tuple[ResearchToolEvidence, ...] = (),
    member_handoffs: tuple[ResearchMemberHandoff, ...] = (),
) -> str:
    """Hash the immutable input, typed draft, raw scores, and tool evidence."""
    payload = {
        "command": command.model_dump(mode="json"),
        "draft": draft.model_dump(mode="json"),
        "raw_scores": (
            tuple(score.model_dump(mode="json") for score in raw_scores)
            if raw_scores is not None
            else None
        ),
        "tool_evidence_refs": tool_evidence_refs,
        "tool_evidence": tuple(evidence.model_dump(mode="json") for evidence in tool_evidence),
        "member_handoffs": tuple(member.model_dump(mode="json") for member in member_handoffs),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def risk_run_id_for(
    research_run_id: str,
    draft: ResearchDraft,
    *,
    raw_scores: tuple[RawScore, ...] | None = None,
    tool_evidence_refs: tuple[str, ...] = (),
    tool_evidence: tuple[ResearchToolEvidence, ...] = (),
    member_handoffs: tuple[ResearchMemberHandoff, ...] = (),
) -> str:
    """Derive the independent risk Run identity from the frozen research Run."""
    digest = sha256(
        json.dumps(
            {
                "research_run_id": research_run_id,
                "draft": draft.model_dump(mode="json"),
                "raw_scores": (
                    tuple(score.model_dump(mode="json") for score in raw_scores)
                    if raw_scores is not None
                    else None
                ),
                "tool_evidence_refs": tool_evidence_refs,
                "tool_evidence": tuple(
                    evidence.model_dump(mode="json") for evidence in tool_evidence
                ),
                "member_handoffs": tuple(
                    member.model_dump(mode="json") for member in member_handoffs
                ),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return f"risk-run-{digest}"


def research_member_results(draft: ResearchDraft) -> tuple[ResearchMemberResult, ...]:
    return tuple(
        ResearchMemberResult(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence_refs=member.evidence_refs,
            thesis=member.thesis,
            bull_case=member.bull_case,
            bear_case=member.bear_case,
            knowledge_cutoff=member.knowledge_cutoff,
        )
        for member in draft.members
    )
