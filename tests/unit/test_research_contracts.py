from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from hashlib import sha256
from typing import Literal, cast

import pytest

from stock_profiler.modules.research.contracts import (
    RAW_SCORE_FEATURE_IDS,
    RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
    RESEARCH_EVIDENCE_CONTRACT_VERSION,
    RESEARCH_LEGACY_DEFINITION_VERSION,
    RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION,
    RESEARCH_PRIOR_DEFINITION_VERSION,
    RESEARCH_REQUIRED_DATA_TYPES,
    FrozenDualTargetScreening,
    RawScoreCalculationError,
    RawScoreFeatureTransform,
    RawScoreModelSnapshot,
    ResearchCommand,
    ResearchDataManifest,
    ResearchDataManifestEntry,
    ResearchDraft,
    ResearchDraftMember,
    ResearchEvidence,
    ResearchFrameworkOutput,
    ResearchMemberHandoff,
    ResearchMemberInput,
    ResearchMoneyFlowFacts,
    ResearchStageArtifact,
    ResearchStructuredFacts,
    ResearchToolEvidence,
    RiskGate,
    RiskMemberVeto,
    RiskVetoDraft,
    calculate_structured_signals,
    decode_historical_research_command,
    decode_historical_research_draft,
    decode_legacy_research_command,
    decode_legacy_research_framework_output,
    decode_legacy_research_outcome,
    decode_legacy_research_stage_artifact,
    freeze_raw_score,
    frozen_raw_score_model_snapshot,
    handoff_fingerprint,
    research_draft_payload,
    research_member_handoff_payload,
    research_outcome_payload,
    research_outcome_raw_score_payloads,
    research_raw_score_payload,
    screening_output_sha256,
    selection_binding_sha256,
)
from stock_profiler.modules.research.service import (
    freeze_research,
    prepare_research_risk_plan,
    validate_research_draft,
)


def _member_input(index: int, cutoff: datetime) -> ResearchMemberInput:
    signal_value = Decimal(index + 1) / Decimal(10)
    structured_facts = ResearchStructuredFacts(
        money_flow=ResearchMoneyFlowFacts(
            net_amount=signal_value,
            inflow_amount=signal_value + Decimal("1"),
            outflow_amount=Decimal("1"),
        ),
        revenue_growth_current=signal_value,
        revenue_growth_prior=Decimal("0"),
        quarter_profit_improvement=signal_value,
        average_total_assets=Decimal("1"),
        operating_cash_flow_ttm=signal_value,
        working_capital_pressure_current=signal_value,
        working_capital_pressure_prior=Decimal("0"),
        leverage_ratio_current=signal_value,
        leverage_ratio_prior=Decimal("0"),
        stock_return_20d=signal_value,
        industry_return_20d=Decimal("0"),
        downside_semivariance_60d=signal_value,
        max_drawdown_60d=signal_value,
        turnover_change=signal_value,
        institutional_net_buy_ratio=signal_value,
        institutional_listing_frequency=signal_value,
    )
    evidence = tuple(
        ResearchEvidence(
            evidence_id=f"{data_type.lower()}-evidence-{index:02}",
            source=f"fictional-{data_type.lower()}-feed",
            reference=f"synthetic://evidence/{data_type.lower()}/{index:02}",
            statement=(
                f"A fictional structured fact is available at the cutoff for {data_type.lower()}."
            ),
            effective_at=cutoff,
            source_published_at=cutoff,
            acquired_at=cutoff,
            validated_at=cutoff,
            knowledge_cutoff=cutoff,
            semantic_version=f"fictional-{data_type.lower()}-feed-v1",
            validation_status="VALIDATED",
        )
        for data_type in RESEARCH_REQUIRED_DATA_TYPES
    )
    return ResearchMemberInput(
        security_id=f"synthetic-security-{index:02}",
        research_id=f"research-{index:02}",
        knowledge_cutoff=cutoff,
        evidence=evidence,
        data_manifest=ResearchDataManifest(
            version="synthetic-per-stock-research-manifest-v1",
            entries=tuple(
                ResearchDataManifestEntry(
                    data_type=data_type,
                    provider_id="synthetic-required-fact-provider",
                    provider_version="synthetic-required-fact-provider-v1",
                    completeness="COMPLETE",
                    event_status="PRESENT",
                    evidence_ids=(evidence[position].evidence_id,),
                    knowledge_cutoff=cutoff,
                )
                for position, data_type in enumerate(RESEARCH_REQUIRED_DATA_TYPES)
            ),
        ),
        structured_facts=structured_facts,
        structured_signals=calculate_structured_signals(structured_facts),
        risk_flags=("LIQUIDITY_WARNING",) if index == 0 else (),
    )


def _command(*, risk_scenario: Literal["ACCEPT", "REJECT"] = "ACCEPT") -> ResearchCommand:
    cutoff = datetime(2042, 6, 30, 23, 59, 59, tzinfo=UTC)
    members = tuple(_member_input(index, cutoff) for index in range(10))
    ids = tuple(member.security_id for member in members)
    screening = FrozenDualTargetScreening.model_construct(
        positive_target="SIX_MONTH_POSITIVE_RETURN",
        terminal_target="SIX_MONTH_TERMINAL_20_PERCENT",
        strategy_version="synthetic-dual-head-strategy-v1",
        snapshot_id="synthetic-dual-head-snapshot-v1",
        universe_security_ids=ids,
        selected_member_ids=ids,
        positive_scores={security_id: Decimal("0.6") for security_id in ids},
        terminal_scores={security_id: Decimal("0.7") for security_id in ids},
        positive_percentiles={security_id: Decimal("60") for security_id in ids},
        terminal_percentiles={security_id: Decimal("70") for security_id in ids},
        positive_head_version="synthetic-positive-head-v1",
        terminal_head_version="synthetic-terminal-head-v1",
        output_sha256="",
    )
    screening = FrozenDualTargetScreening.model_validate(
        screening.model_copy(
            update={"output_sha256": screening_output_sha256(screening)}
        ).model_dump(mode="python")
    )
    return ResearchCommand(
        contract_version="1.0.0",
        synthetic=True,
        generator_version="synthetic-research-v1",
        seed=1616,
        selection_object_id="selection-object-1616",
        selection_event_id="selection-event-1616",
        selection_fingerprint=selection_binding_sha256(
            "selection-object-1616",
            "selection-event-1616",
            cutoff,
            screening,
        ),
        cutoff_at=cutoff,
        knowledge_cutoff=cutoff,
        purpose="SYNTHETIC",
        screening=screening,
        members=members,
        raw_score_model=frozen_raw_score_model_snapshot(),
        risk_scenario=risk_scenario,
    )


def test_research_command_freezes_ten_independent_member_identities() -> None:
    command = _command()

    assert len(command.members) == 10
    assert len({member.research_id for member in command.members}) == 10
    assert command.screening.selected_member_ids == tuple(
        member.security_id for member in command.members
    )
    assert all(
        member.evidence[0].knowledge_cutoff == command.knowledge_cutoff
        for member in command.members
    )


def test_raw_score_is_a_structured_uncalibrated_z20_with_no_text_input() -> None:
    command = _command()
    raw_score = freeze_raw_score(command, command.members[0])

    assert raw_score.target == "SIX_MONTH_TERMINAL_20_PERCENT"
    assert raw_score.probability is None
    assert raw_score.model_version == "elastic-net-logistic-z20-v1"
    assert raw_score.training_window_id == "synthetic-training-window-expanding-60m"
    assert raw_score.training_window_kind == "EXPANDING"
    assert raw_score.training_window_month_count == 60
    assert raw_score.training_window_start_month == "2036-12"
    assert raw_score.training_window_end_month == "2041-11"
    assert raw_score.label_watermark_month == "2042-06"
    assert len(command.raw_score_model.training_records) == 524
    assert all(
        record.evaluation_entry_at is None
        or record.selection_cutoff_at < record.evaluation_entry_at
        for record in command.raw_score_model.training_records
    )
    assert any(
        record.evaluation_entry_at is None for record in command.raw_score_model.training_records
    )
    assert (
        max(record.label_available_at for record in command.raw_score_model.training_records)
        == command.raw_score_model.label_watermark_at
    )
    assert raw_score.normalization_snapshot_id == "synthetic-normalization-v1"
    assert raw_score.interaction_terms == ()
    assert raw_score.z20 == Decimal("33.127")
    assert raw_score.transformed_inputs["working_capital_pressure_change"] == Decimal("-0.1")
    assert raw_score.feature_transformations["working_capital_pressure_change"].reverse is True


def test_raw_score_model_snapshot_requires_the_frozen_training_waterline() -> None:
    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["mature_months"] = 59

    with pytest.raises(ValueError, match="mature month count"):
        ResearchCommand.model_validate(payload)

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["training_record_count"] = 499
    payload["raw_score_model"]["positive_record_count"] = 249

    with pytest.raises(ValueError, match="training record count"):
        ResearchCommand.model_validate(payload)


def test_raw_score_snapshot_freezes_transformations_and_model_constraints() -> None:
    snapshot = frozen_raw_score_model_snapshot()

    assert set(snapshot.transformations) == set(RAW_SCORE_FEATURE_IDS)
    assert snapshot.transformations["working_capital_pressure_change"].reverse is True
    assert snapshot.transformations["turnover_change"].reverse is False
    assert snapshot.l1_ratio == Decimal("0.25")
    assert snapshot.l2_ratio == Decimal("0.75")
    assert snapshot.fit_diagnostics.status == "SYNTHETIC_NOT_FIT"
    assert snapshot.fit_diagnostics.training_log_loss is None
    assert len(snapshot.code_sha256) == 64
    assert len(snapshot.model_artifact_sha256) == 64
    assert len(snapshot.environment_sha256) == 64
    assert snapshot.randomness_control == "deterministic-synthetic-seed-1616"

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["l1_ratio"] = "1"
    payload["raw_score_model"]["l2_ratio"] = "0"
    with pytest.raises(ValueError, match="25% L1 and 75% L2"):
        ResearchCommand.model_validate(payload)

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["coefficients"]["turnover_change"] = "-0.02"
    payload["raw_score_model"]["coefficients"]["institutional_listing_frequency"] = "-0.03"
    ResearchCommand.model_validate(payload)

    payload["raw_score_model"]["coefficients"]["institutional_net_buy_ratio"] = "-0.03"
    with pytest.raises(ValueError, match="non-negative"):
        ResearchCommand.model_validate(payload)

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["transformations"]["downside_semivariance_60d"]["reverse"] = False
    with pytest.raises(ValueError, match="direction"):
        ResearchCommand.model_validate(payload)


def test_raw_score_rejects_a_model_artifact_hash_that_does_not_match_parameters() -> None:
    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["coefficients"]["turnover_change"] = "0.99"
    command = ResearchCommand.model_validate(payload)

    with pytest.raises(RawScoreCalculationError, match="RAW_SCORE_MODEL_ARTIFACT_MISMATCH"):
        freeze_raw_score(command, command.members[0])


def test_current_raw_score_snapshot_requires_audit_provenance() -> None:
    payload = _command().model_dump(mode="json")
    for field_name in (
        "fit_diagnostics",
        "code_sha256",
        "model_artifact_sha256",
        "environment_sha256",
        "randomness_control",
    ):
        payload["raw_score_model"].pop(field_name)

    with pytest.raises(ValueError):
        ResearchCommand.model_validate(payload)


def test_raw_score_snapshot_freezes_temporal_window_and_penalty_policy() -> None:
    snapshot = frozen_raw_score_model_snapshot()

    assert len(snapshot.training_months) == 60
    assert snapshot.training_months[0] == snapshot.training_window_start_month
    assert snapshot.training_months[-1] == snapshot.training_window_end_month
    assert snapshot.label_watermark_month == "2042-06"
    assert snapshot.training_window_policy == "EXPANDING_60_TO_119_ROLLING_120"

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["mature_months"] = 121
    with pytest.raises(ValueError, match="mature month count"):
        ResearchCommand.model_validate(payload)

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["penalty_strength"] = "999"
    with pytest.raises(ValueError, match="penalty strength"):
        ResearchCommand.model_validate(payload)

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["training_records"][-1]["label_available_at"] = (
        "2042-07-01T16:00:00+00:00"
    )
    payload["raw_score_model"].update(
        {
            "label_watermark_month": "2042-07",
            "label_watermark_at": "2042-07-01T16:00:00+00:00",
        }
    )
    with pytest.raises(ValueError, match="research cutoff"):
        ResearchCommand.model_validate(payload)


def test_raw_score_snapshot_binds_mature_label_evidence() -> None:
    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["training_records"][0]["label_available_at"] = (
        "2037-06-30T16:00:00+00:00"
    )
    with pytest.raises(ValueError, match="maturity"):
        ResearchCommand.model_validate(payload)

    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["training_record_count"] += 1
    with pytest.raises(ValueError, match="training record count"):
        ResearchCommand.model_validate(payload)


def test_raw_score_snapshot_must_follow_the_mature_window_for_the_cutoff() -> None:
    payload = _command().model_dump(mode="json")
    future_cutoff = datetime(2048, 6, 30, 23, 59, 59, tzinfo=UTC)
    old_cutoff = "2042-06-30T23:59:59Z"
    new_cutoff = future_cutoff.isoformat().replace("+00:00", "Z")

    def replace_cutoff(value: object) -> object:
        if isinstance(value, dict):
            return {key: replace_cutoff(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_cutoff(item) for item in value]
        return new_cutoff if value == old_cutoff else value

    payload = cast(dict[str, object], replace_cutoff(payload))
    screening = FrozenDualTargetScreening.model_validate(payload["screening"])
    payload["selection_fingerprint"] = selection_binding_sha256(
        cast(str, payload["selection_object_id"]),
        cast(str, payload["selection_event_id"]),
        future_cutoff,
        screening,
    )

    with pytest.raises(ValueError, match="mature training window"):
        ResearchCommand.model_validate(payload)


def test_historical_research_command_preserves_its_original_mature_window() -> None:
    payload = _command().model_dump(mode="json")
    historical_cutoff = datetime(2042, 7, 31, 23, 59, 59, tzinfo=UTC)
    old_cutoff = "2042-06-30T23:59:59Z"
    new_cutoff = historical_cutoff.isoformat().replace("+00:00", "Z")

    def replace_cutoff(value: object) -> object:
        if isinstance(value, dict):
            return {key: replace_cutoff(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_cutoff(item) for item in value]
        return new_cutoff if value == old_cutoff else value

    payload = cast(dict[str, object], replace_cutoff(payload))
    screening = FrozenDualTargetScreening.model_validate(payload["screening"])
    payload["selection_fingerprint"] = selection_binding_sha256(
        cast(str, payload["selection_object_id"]),
        cast(str, payload["selection_event_id"]),
        historical_cutoff,
        screening,
    )
    raw_score_model = cast(dict[str, object], payload["raw_score_model"])
    training_cohorts = cast(list[dict[str, object]], raw_score_model["training_cohorts"])
    for cohort in training_cohorts:
        cohort["research_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION

    decoded = decode_historical_research_command(payload)

    assert decoded.cutoff_at == historical_cutoff
    assert decoded.raw_score_model.training_months[-1] == "2041-11"
    assert decoded.raw_score_model.training_window_month_count == 60


def test_historical_research_command_recovers_missing_audit_provenance() -> None:
    payload = _command().model_dump(mode="json")
    for cohort in payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    for field_name in (
        "fit_diagnostics",
        "code_sha256",
        "model_artifact_sha256",
        "environment_sha256",
        "randomness_control",
    ):
        payload["raw_score_model"].pop(field_name)

    decoded = decode_historical_research_command(payload)

    assert decoded.raw_score_model.fit_diagnostics.status == "SYNTHETIC_NOT_FIT"
    assert len(decoded.raw_score_model.code_sha256) == 64
    assert len(decoded.raw_score_model.model_artifact_sha256) == 64
    assert len(decoded.raw_score_model.environment_sha256) == 64
    assert decoded.raw_score_model.randomness_control == ("deterministic-synthetic-seed-1616")


def test_historical_research_command_accepts_the_pre_money_flow_fact_shape() -> None:
    payload = _command().model_dump(mode="json")
    for cohort in payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    for member in payload["members"]:
        member["structured_facts"].pop("money_flow", None)

    decoded = decode_historical_research_command(payload)

    assert all(member._historical_decoded for member in decoded.members)
    assert all(not member.structured_facts.money_flow.is_complete for member in decoded.members)


def test_historical_handoff_fingerprint_preserves_the_original_command_payload() -> None:
    payload = _command().model_dump(mode="json")
    for cohort in payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION
    for field_name in (
        "fit_diagnostics",
        "code_sha256",
        "model_artifact_sha256",
        "environment_sha256",
        "randomness_control",
    ):
        payload["raw_score_model"].pop(field_name)

    decoded = decode_historical_research_command(payload)
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
                thesis="The historical thesis remains bounded by frozen evidence.",
                bull_case="The historical upside case remains conditional.",
                bear_case="The historical downside case remains explicit.",
                catalysts=("A historical catalyst remains conditional.",),
                falsification_conditions=("A historical downside fact would falsify the thesis.",),
                unknowns=("Historical future evidence remains unresolved.",),
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in decoded.members
        ),
    )
    raw_scores = tuple(freeze_raw_score(decoded, member) for member in decoded.members)
    expected_payload = {
        "command": payload,
        "draft": research_draft_payload(draft, historical=True),
        "raw_scores": tuple(research_raw_score_payload(score) for score in raw_scores),
        "tool_evidence_refs": (),
        "tool_evidence": (),
        "member_handoffs": (),
    }
    expected_fingerprint = sha256(
        json.dumps(expected_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert (
        handoff_fingerprint(
            decoded,
            draft,
            raw_scores=raw_scores,
            historical=True,
        )
        == expected_fingerprint
    )


def test_legacy_research_command_decodes_the_original_input_shape() -> None:
    payload = _command().model_dump(mode="json")
    for member in payload["members"]:
        member.pop("structured_facts", None)
        for evidence in member["evidence"]:
            evidence.pop("evidence_contract_version", None)
            evidence.pop("effective_at", None)
            evidence.pop("source_published_at", None)
    payload["raw_score_model"].pop("label_watermark_at", None)
    payload["raw_score_model"].pop("training_cohorts", None)
    payload["raw_score_model"].pop("training_records", None)

    decoded = decode_legacy_research_command(payload)

    assert decoded.members[0].structured_signals == _command().members[0].structured_signals
    assert decoded.raw_score_model.training_cohorts == ()
    assert decoded.raw_score_model.training_records == ()
    assert decoded.raw_score_model.label_watermark_at is not None


def test_legacy_research_command_decodes_the_original_v2_signal_schema() -> None:
    legacy_signal_ids = (
        "revenue_growth",
        "earnings_revision",
        "free_cash_flow_margin",
        "leverage_ratio",
        "valuation_gap",
        "price_trend_6m",
        "volatility_20d",
        "drawdown_6m",
        "breakout_distance",
        "path_consistency",
        "level2_imbalance",
    )
    payload = _command().model_dump(mode="json")
    payload.pop("selection_fingerprint")
    payload["screening"].pop("positive_percentiles")
    payload["screening"].pop("terminal_percentiles")
    payload["screening"]["output_sha256"] = "a" * 64
    payload["raw_score_model"] = None
    for member in payload["members"]:
        member["evidence"] = [member["evidence"][0]]
        member.pop("data_manifest")
        member.pop("structured_facts")
        member["structured_signals"] = {signal_id: "0.01" for signal_id in legacy_signal_ids}
        for evidence in member["evidence"]:
            for field_name in (
                "evidence_contract_version",
                "effective_at",
                "source_published_at",
                "acquired_at",
                "validated_at",
                "semantic_version",
                "validation_status",
            ):
                evidence.pop(field_name, None)

    decoded = decode_legacy_research_command(payload)
    raw_score = freeze_raw_score(decoded, decoded.members[0])

    assert decoded.members[0]._legacy_structured_signals is not None
    assert tuple(decoded.members[0]._legacy_structured_signals) == legacy_signal_ids
    assert raw_score.structured_inputs["revenue_growth"] == Decimal("0.01")
    assert raw_score.coefficients["leverage_ratio"] == Decimal("-0.05")
    assert raw_score.z20 == Decimal("-0.0615")


def test_legacy_research_command_restores_missing_percentiles_for_the_full_universe() -> None:
    payload = _command().model_dump(mode="json")
    extra_security_id = "synthetic-security-extra"
    screening = payload["screening"]
    screening["universe_security_ids"].append(extra_security_id)
    screening["positive_scores"][extra_security_id] = "0.5"
    screening["terminal_scores"][extra_security_id] = "0.5"
    screening.pop("positive_percentiles")
    screening.pop("terminal_percentiles")
    screening["output_sha256"] = "a" * 64
    payload.pop("selection_fingerprint")
    payload["raw_score_model"] = None

    decoded = decode_legacy_research_command(payload)

    universe_ids = set(decoded.screening.universe_security_ids)
    assert set(decoded.screening.positive_percentiles) == universe_ids
    assert set(decoded.screening.terminal_percentiles) == universe_ids
    assert decoded.screening.positive_percentiles[extra_security_id] == Decimal("0")
    assert decoded.screening.terminal_percentiles[extra_security_id] == Decimal("0")


def test_legacy_research_command_recovers_missing_audit_provenance() -> None:
    payload = _command().model_dump(mode="json")
    for cohort in payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = RESEARCH_LEGACY_DEFINITION_VERSION
    for field_name in (
        "fit_diagnostics",
        "code_sha256",
        "model_artifact_sha256",
        "environment_sha256",
        "randomness_control",
    ):
        payload["raw_score_model"].pop(field_name)

    decoded = decode_legacy_research_command(payload)

    assert decoded.raw_score_model.fit_diagnostics.status == "SYNTHETIC_NOT_FIT"
    assert len(decoded.raw_score_model.code_sha256) == 64
    assert len(decoded.raw_score_model.model_artifact_sha256) == 64
    assert len(decoded.raw_score_model.environment_sha256) == 64
    assert decoded.raw_score_model.randomness_control == ("deterministic-synthetic-seed-1616")


def test_legacy_research_command_preserves_existing_training_provenance() -> None:
    payload = _command().model_dump(mode="json")
    for cohort in payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = RESEARCH_LEGACY_DEFINITION_VERSION

    decoded = decode_legacy_research_command(payload)

    assert len(decoded.raw_score_model.training_cohorts) == 60
    assert len(decoded.raw_score_model.training_records) == 524
    assert (
        decoded.raw_score_model.training_cohorts[0].research_definition_version
        == RESEARCH_LEGACY_DEFINITION_VERSION
    )


def test_pre_calibration_research_snapshot_remains_replayable() -> None:
    payload = _command().raw_score_model.model_dump(mode="json")
    payload.pop("calibration_evidence_version")
    for record in payload["training_records"]:
        for field_name in (
            "raw_score_frozen_at",
            "raw_score_training_watermark_at",
            "raw_success_score",
            "historical_calibrated_probability",
            "entry_window_ends_at",
            "unified_maturity_at",
        ):
            record.pop(field_name)

    snapshot = RawScoreModelSnapshot.model_validate(payload)

    assert len(snapshot.training_records) == 524
    assert snapshot.training_records[0].raw_success_score is None


def test_v1_calibration_snapshot_remains_replayable_without_oos_probability() -> None:
    payload = _command().raw_score_model.model_dump(mode="json")
    payload["calibration_evidence_version"] = "frozen-oos-calibration-v1"
    for record in payload["training_records"]:
        record.pop("historical_calibrated_probability")

    snapshot = RawScoreModelSnapshot.model_validate(payload)

    assert snapshot.calibration_evidence_version == "frozen-oos-calibration-v1"
    assert snapshot.training_records[0].historical_calibrated_probability is None


def test_current_raw_score_snapshot_rejects_partial_calibration_evidence() -> None:
    payload = _command().raw_score_model.model_dump(mode="json")
    payload["training_records"][0].pop("raw_success_score")

    with pytest.raises(ValueError, match="raw-score calibration evidence is incomplete"):
        RawScoreModelSnapshot.model_validate(payload)


def test_legacy_research_command_decodes_pre_cohort_training_records() -> None:
    payload = _command().model_dump(mode="json")
    raw_score_model = payload["raw_score_model"]
    raw_score_model.pop("training_cohorts", None)
    for record in raw_score_model["training_records"]:
        record.pop("cohort_id", None)
        record.pop("evaluation_entry_at", None)

    decoded = decode_legacy_research_command(payload)

    assert len(decoded.raw_score_model.training_records) == 524
    assert decoded.raw_score_model.training_records[0].cohort_id.startswith(
        "legacy-training-cohort-"
    )
    evaluation_entry_at = decoded.raw_score_model.training_records[0].evaluation_entry_at
    assert evaluation_entry_at is not None
    assert evaluation_entry_at > decoded.raw_score_model.training_records[0].selection_cutoff_at


def test_legacy_risk_plan_preserves_the_original_raw_score_identity() -> None:
    command = _command()
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
                thesis="The historical thesis remains bounded by frozen evidence.",
                bull_case="The historical upside case remains conditional.",
                bear_case="The historical downside case remains explicit.",
                knowledge_cutoff=member.knowledge_cutoff,
                catalysts=("A historical catalyst remains conditional.",),
                falsification_conditions=("A historical falsifier remains explicit.",),
                unknowns=("A historical unknown remains unresolved.",),
            )
            for member in command.members
        ),
    )
    member_handoffs = tuple(
        ResearchMemberHandoff(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence=member.evidence,
            risk_flags=member.risk_flags,
        )
        for member in command.members
    )

    plan = prepare_research_risk_plan(
        command,
        "research-run-legacy",
        draft,
        (),
        legacy=True,
    )
    risk_payload = json.loads(plan.input_payload)

    assert "label_watermark_at" not in risk_payload["raw_scores"][0]
    expected_raw_scores = tuple(
        {
            key: value
            for key, value in score.model_dump(mode="json").items()
            if key != "label_watermark_at"
        }
        for score in plan.raw_scores
    )
    expected_payload = {
        "research_run_id": "research-run-legacy",
        "draft": research_draft_payload(draft, legacy=True),
        "raw_scores": expected_raw_scores,
        "tool_evidence_refs": (),
        "tool_evidence": (),
        "member_handoffs": tuple(
            research_member_handoff_payload(member, legacy=True) for member in member_handoffs
        ),
    }
    expected_risk_run_id = (
        "risk-run-"
        + sha256(
            json.dumps(
                expected_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    assert plan.risk_run_id == expected_risk_run_id

    historical_raw_score_payloads = tuple(
        research_raw_score_payload(score) for score in plan.raw_scores
    )
    historical_plan = prepare_research_risk_plan(
        command,
        "research-run-legacy",
        draft,
        (),
        legacy=True,
        raw_score_payloads=historical_raw_score_payloads,
    )
    historical_risk_payload = json.loads(historical_plan.input_payload)

    assert "label_watermark_at" in historical_risk_payload["raw_scores"][0]
    expected_historical_payload = {
        **expected_payload,
        "raw_scores": historical_raw_score_payloads,
    }
    expected_historical_risk_run_id = (
        "risk-run-"
        + sha256(
            json.dumps(
                expected_historical_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
    )
    assert historical_plan.risk_run_id == expected_historical_risk_run_id
    assert historical_plan.risk_run_id != plan.risk_run_id

    tampered_raw_score_payloads = list(historical_raw_score_payloads)
    tampered_raw_score_payloads[0] = {
        **tampered_raw_score_payloads[0],
        "z20": "999",
    }
    with pytest.raises(RawScoreCalculationError, match="RAW_SCORE_INPUT_MISMATCH"):
        prepare_research_risk_plan(
            command,
            "research-run-legacy",
            draft,
            (),
            legacy=True,
            raw_score_payloads=tuple(tampered_raw_score_payloads),
        )


def test_legacy_outcome_replays_original_v2_scores_into_risk_plan() -> None:
    payload = _command().model_dump(mode="json")
    payload.pop("selection_fingerprint")
    payload["screening"].pop("positive_percentiles")
    payload["screening"].pop("terminal_percentiles")
    payload["screening"]["output_sha256"] = "a" * 64
    payload["raw_score_model"] = None
    legacy_signal_ids = (
        "revenue_growth",
        "earnings_revision",
        "free_cash_flow_margin",
        "leverage_ratio",
        "valuation_gap",
        "price_trend_6m",
        "volatility_20d",
        "drawdown_6m",
        "breakout_distance",
        "path_consistency",
        "level2_imbalance",
    )
    for member in payload["members"]:
        member["evidence"] = [member["evidence"][0]]
        member.pop("data_manifest")
        member.pop("structured_facts")
        member["structured_signals"] = {signal_id: "0.01" for signal_id in legacy_signal_ids}
        for evidence in member["evidence"]:
            for field_name in (
                "evidence_contract_version",
                "effective_at",
                "source_published_at",
                "acquired_at",
                "validated_at",
                "semantic_version",
                "validation_status",
            ):
                evidence.pop(field_name, None)

    command = decode_legacy_research_command(payload)
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
                thesis="The v2 thesis remains bounded by frozen evidence.",
                bull_case="The v2 upside case remains conditional.",
                bear_case="The v2 downside case remains explicit.",
                catalysts=("A v2 catalyst remains conditional.",),
                falsification_conditions=("A v2 downside fact would falsify the thesis.",),
                unknowns=("V2 future evidence remains unresolved.",),
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in command.members
        ),
    )
    raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    member_handoffs = tuple(
        ResearchMemberHandoff(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence=member.evidence,
            risk_flags=member.risk_flags,
        )
        for member in command.members
    )
    risk_veto = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff_fingerprint(
            command,
            draft,
            raw_scores=raw_scores,
            member_handoffs=member_handoffs,
            legacy=True,
        ),
        disposition="REJECTED",
        gates=(RiskGate(gate_id="V2_RISK_GATE", status="FAILED"),),
        reasons=("V2_RISK_VETO",),
        member_vetoes=tuple(
            RiskMemberVeto(
                security_id=member.security_id,
                research_id=member.research_id,
                disposition="REJECTED",
                gates=(RiskGate(gate_id="V2_RISK_GATE", status="FAILED"),),
                reasons=("V2_RISK_VETO",),
            )
            for member in command.members
        ),
    )
    outcome = freeze_research(
        command,
        ResearchFrameworkOutput(
            research_run_id="research-run-v2",
            risk_run_id="risk-run-v2",
            draft=draft,
            risk_veto=risk_veto,
            raw_scores=raw_scores,
            member_handoffs=member_handoffs,
        ),
        legacy=True,
    )

    legacy_outcome_payload = research_outcome_payload(outcome, legacy=True)
    decoded_outcome = decode_legacy_research_outcome(legacy_outcome_payload)
    historical_raw_score_payloads = research_outcome_raw_score_payloads(decoded_outcome)

    assert historical_raw_score_payloads is not None
    assert "label_watermark_at" not in historical_raw_score_payloads[0]
    legacy_coefficients = historical_raw_score_payloads[0].get("coefficients")
    assert isinstance(legacy_coefficients, dict)
    assert legacy_coefficients["revenue_growth"] == "0.08"

    plan = prepare_research_risk_plan(
        command,
        "research-run-v2",
        draft,
        (),
        legacy=True,
        raw_score_payloads=historical_raw_score_payloads,
    )
    risk_payload = json.loads(plan.input_payload)

    assert len(plan.raw_scores) == 10
    assert plan.raw_scores[0].z20 == Decimal("-0.0615")
    assert "label_watermark_at" not in risk_payload["raw_scores"][0]
    assert plan.raw_scores[0]._persisted_payload == historical_raw_score_payloads[0]

    tampered_raw_score_payloads = list(historical_raw_score_payloads)
    tampered_raw_score_payloads[0] = {
        **tampered_raw_score_payloads[0],
        "z20": "999",
    }
    with pytest.raises(RawScoreCalculationError, match="RAW_SCORE_INPUT_MISMATCH"):
        prepare_research_risk_plan(
            command,
            "research-run-v2",
            draft,
            (),
            legacy=True,
            raw_score_payloads=tuple(tampered_raw_score_payloads),
        )


def test_legacy_stage_artifact_decodes_without_new_input_item_ids() -> None:
    decoded = decode_legacy_research_stage_artifact(
        {
            "stage_id": "analyze",
            "source_stage_id": "collect",
            "security_ids": ["synthetic-security-00"],
            "evidence_ids": ["daily_market-evidence-00"],
            "summary": "Historical analysis stage.",
        }
    )

    assert decoded.input_item_ids == ()
    assert decoded.security_ids == ("synthetic-security-00",)
    assert decoded.evidence_ids == ("daily_market-evidence-00",)


def test_raw_score_snapshot_rejects_duplicate_security_month_evidence() -> None:
    payload = _command().model_dump(mode="json")
    first_record = payload["raw_score_model"]["training_records"][0]
    second_record = payload["raw_score_model"]["training_records"][1]
    second_record["security_id"] = first_record["security_id"]

    with pytest.raises(ValueError, match="security-month"):
        ResearchCommand.model_validate(payload)


def test_raw_score_snapshot_rejects_record_outside_frozen_training_cohort() -> None:
    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["training_records"][0]["security_id"] = (
        "synthetic-training-security-outside-cohort"
    )

    with pytest.raises(ValueError, match="frozen cohort"):
        ResearchCommand.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("research_definition_id", "synthetic-monthly-research-other"),
        ("research_definition_version", "5.0.0"),
    ),
)
def test_raw_score_snapshot_requires_compatible_research_definition(
    field: str,
    value: str,
) -> None:
    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["training_cohorts"][0][field] = value

    with pytest.raises(ValueError, match="supported research Definition"):
        ResearchCommand.model_validate(payload)


def test_research_signals_must_match_deterministic_source_facts() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"][0]["structured_facts"]["stock_return_20d"] = "9"

    with pytest.raises(ValueError, match="deterministic feature calculator"):
        ResearchCommand.model_validate(payload)


def test_research_evidence_requires_individual_three_clock_timestamps() -> None:
    payload = _command().model_dump(mode="json")
    evidence = payload["members"][0]["evidence"][0]
    evidence["evidence_contract_version"] = RESEARCH_EVIDENCE_CONTRACT_VERSION
    evidence.pop("effective_at", None)
    evidence.pop("source_published_at", None)

    with pytest.raises(ValueError, match="all evidence clocks"):
        ResearchCommand.model_validate(payload)


def test_research_evidence_without_a_version_cannot_skip_clock_validation() -> None:
    payload = _command().model_dump(mode="json")
    evidence = payload["members"][0]["evidence"][0]
    evidence.pop("evidence_contract_version", None)
    evidence.pop("effective_at", None)
    evidence.pop("source_published_at", None)

    with pytest.raises(ValueError, match="all evidence clocks"):
        ResearchCommand.model_validate(payload)


def test_current_research_evidence_cannot_use_legacy_version_to_skip_cutoff_validation() -> None:
    payload = _command().model_dump(mode="json")
    evidence = payload["members"][0]["evidence"][0]
    evidence["evidence_contract_version"] = RESEARCH_LEGACY_EVIDENCE_CONTRACT_VERSION
    evidence["acquired_at"] = "2042-07-01T00:00:00Z"
    evidence["validated_at"] = "2042-07-01T00:00:00Z"

    with pytest.raises(ValueError, match="available by the cutoff|historical"):
        ResearchCommand.model_validate(payload)


def test_legacy_research_evidence_decodes_without_new_clock_fields() -> None:
    payload = _command().model_dump(mode="json")
    evidence = payload["members"][0]["evidence"][0]
    evidence.pop("evidence_contract_version", None)
    evidence.pop("effective_at", None)
    evidence.pop("source_published_at", None)

    with pytest.raises(ValueError, match="all evidence clocks"):
        ResearchCommand.model_validate(payload)


def test_research_draft_requires_catalysts_falsification_conditions_and_unknowns() -> None:
    draft_member = {
        "security_id": "synthetic-security-00",
        "research_id": "research-00",
        "evidence_refs": ["daily_market-evidence-00"],
        "thesis": "Synthetic thesis.",
        "bull_case": "Synthetic bull case.",
        "bear_case": "Synthetic bear case.",
        "knowledge_cutoff": "2042-06-30T23:59:59Z",
    }
    draft_payload = {
        "contract_version": "1.0.0",
        "members": [draft_member for _ in range(10)],
    }

    with pytest.raises(ValueError):
        ResearchDraft.model_validate(draft_payload)


def test_raw_score_training_rejects_mixed_research_definition_versions() -> None:
    payload = _command().model_dump(mode="json")
    payload["raw_score_model"]["training_cohorts"][0]["research_definition_version"] = "2.0.0"

    with pytest.raises(ValueError, match="same research Definition"):
        ResearchCommand.model_validate(payload)


def test_raw_score_training_definition_matches_the_executing_research_definition() -> None:
    payload = _command().model_dump(mode="json")
    for cohort in payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = "2.0.0"

    with pytest.raises(ValueError, match="match the executing research Definition"):
        ResearchCommand.model_validate(payload)


def test_legacy_framework_output_decodes_the_historical_draft_shape() -> None:
    command = _command()
    payload = {
        "research_run_id": "research-run-legacy",
        "draft": {
            "contract_version": "1.0.0",
            "members": [
                {
                    "security_id": member.security_id,
                    "research_id": member.research_id,
                    "evidence_refs": [evidence.evidence_id for evidence in member.evidence],
                    "thesis": "The historical thesis is bounded by frozen evidence.",
                    "bull_case": "The historical upside case remains conditional.",
                    "bear_case": "The historical downside case remains explicit.",
                    "knowledge_cutoff": member.knowledge_cutoff.isoformat(),
                }
                for member in command.members
            ],
        },
    }

    decoded = decode_legacy_research_framework_output(payload)

    assert decoded.draft.members[0].catalysts == ("HISTORICAL_CONTRACT_FIELD_NOT_RECORDED",)
    assert decoded.draft.members[0].falsification_conditions == (
        "HISTORICAL_CONTRACT_FIELD_NOT_RECORDED",
    )
    assert decoded.draft.members[0].unknowns == ("HISTORICAL_CONTRACT_FIELD_NOT_RECORDED",)


def test_historical_current_draft_decodes_without_erasing_current_evidence_contract() -> None:
    command = _command()
    payload = {
        "contract_version": "1.0.0",
        "members": [
            {
                "security_id": member.security_id,
                "research_id": member.research_id,
                "evidence_refs": [evidence.evidence_id for evidence in member.evidence],
                "thesis": "The historical current thesis is bounded by frozen evidence.",
                "bull_case": "The historical current upside case remains conditional.",
                "bear_case": "The historical current downside case remains explicit.",
                "catalysts": ["The historical catalyst remains explicit."],
                "falsification_conditions": ["The historical falsifier remains explicit."],
                "unknowns": ["The historical unknown remains explicit."],
                "knowledge_cutoff": member.knowledge_cutoff.isoformat(),
            }
            for member in command.members
        ],
    }

    decoded = decode_historical_research_draft(payload)

    assert decoded.members[0].catalysts == ("The historical catalyst remains explicit.",)
    assert decoded.members[0].falsification_conditions == (
        "The historical falsifier remains explicit.",
    )
    assert decoded.members[0].unknowns == ("The historical unknown remains explicit.",)


def test_current_research_command_does_not_accept_the_prior_definition_version() -> None:
    payload = _command().model_dump(mode="json")
    for cohort in payload["raw_score_model"]["training_cohorts"]:
        cohort["research_definition_version"] = RESEARCH_PRIOR_DEFINITION_VERSION

    with pytest.raises(ValueError, match="match the executing research Definition"):
        ResearchCommand.model_validate(payload)


def test_working_capital_signal_is_normalized_by_average_total_assets() -> None:
    facts = _member_input(0, datetime(2042, 6, 30, 23, 59, 59, tzinfo=UTC)).structured_facts
    facts = facts.model_copy(
        update={
            "working_capital_pressure_current": Decimal("30"),
            "working_capital_pressure_prior": Decimal("10"),
            "average_total_assets": Decimal("100"),
        }
    )

    with localcontext(Context(prec=38)):
        assert calculate_structured_signals(facts)["working_capital_pressure_change"] == Decimal(
            "0.2"
        )


def test_structured_signal_calculation_is_independent_of_ambient_decimal_precision() -> None:
    facts = _member_input(0, datetime(2042, 6, 30, 23, 59, 59, tzinfo=UTC)).structured_facts
    facts = facts.model_copy(
        update={
            "quarter_profit_improvement": Decimal("1"),
            "average_total_assets": Decimal("7"),
            "operating_cash_flow_ttm": Decimal("1"),
            "working_capital_pressure_current": Decimal("30"),
            "working_capital_pressure_prior": Decimal("10"),
        }
    )

    with localcontext(Context(prec=6)):
        low_precision = calculate_structured_signals(facts)
    with localcontext(Context(prec=50)):
        high_precision = calculate_structured_signals(facts)

    assert low_precision == high_precision


def test_raw_score_feature_transform_rejects_invalid_frozen_parameters() -> None:
    with pytest.raises(ValueError, match="clip bounds"):
        RawScoreFeatureTransform(
            lower_clip=Decimal("1"),
            upper_clip=Decimal("1"),
            median=Decimal("1"),
            iqr=Decimal("1"),
            reverse=False,
        )


def test_raw_score_does_not_change_when_research_text_changes() -> None:
    command = _command()
    original = freeze_raw_score(command, command.members[0])
    changed_member = command.members[0].model_copy(
        update={
            "structured_signals": command.members[0].structured_signals,
            "risk_flags": (),
        }
    )

    assert freeze_raw_score(command, changed_member) == original


def test_raw_score_uses_frozen_percentiles_not_screening_head_scores() -> None:
    command = _command()
    original = freeze_raw_score(command, command.members[0])
    changed_screening = command.screening.model_copy(
        update={
            "positive_scores": {
                security_id: Decimal("0.9")
                for security_id in command.screening.universe_security_ids
            },
            "terminal_scores": {
                security_id: Decimal("0.1")
                for security_id in command.screening.universe_security_ids
            },
        }
    )
    changed_screening = FrozenDualTargetScreening.model_validate(
        changed_screening.model_copy(
            update={"output_sha256": screening_output_sha256(changed_screening)}
        ).model_dump(mode="python")
    )
    changed_command = command.model_copy(
        update={
            "screening": changed_screening,
            "selection_fingerprint": selection_binding_sha256(
                command.selection_object_id,
                command.selection_event_id,
                command.cutoff_at,
                changed_screening,
            ),
        }
    )

    assert freeze_raw_score(changed_command, changed_command.members[0]) == original


def test_raw_score_changes_when_a_frozen_percentile_changes() -> None:
    command = _command()
    original = freeze_raw_score(command, command.members[0])
    changed_percentiles = command.screening.model_copy(
        update={
            "positive_percentiles": {
                **command.screening.positive_percentiles,
                command.members[0].security_id: Decimal("61"),
            }
        }
    )
    changed_percentiles = FrozenDualTargetScreening.model_validate(
        changed_percentiles.model_copy(
            update={"output_sha256": screening_output_sha256(changed_percentiles)}
        ).model_dump(mode="python")
    )
    changed_command = command.model_copy(
        update={
            "screening": changed_percentiles,
            "selection_fingerprint": selection_binding_sha256(
                command.selection_object_id,
                command.selection_event_id,
                command.cutoff_at,
                changed_percentiles,
            ),
        }
    )

    assert freeze_raw_score(changed_command, changed_command.members[0]).z20 == (
        original.z20 + Decimal("0.15")
    )


def test_screening_output_hash_rejects_a_changed_score() -> None:
    command = _command()
    payload = command.screening.model_dump(mode="json")
    payload["terminal_scores"]["synthetic-security-00"] = "0.8"

    with pytest.raises(ValueError, match="screening output hash"):
        FrozenDualTargetScreening.model_validate(payload)


def test_raw_score_arithmetic_failure_is_a_scoped_domain_failure() -> None:
    command = _command()
    coefficients = dict(command.raw_score_model.coefficients)
    coefficients["single_quarter_revenue_acceleration"] = Decimal("1e1000002")
    model = command.raw_score_model.model_copy(update={"coefficients": coefficients})
    command = command.model_copy(update={"raw_score_model": model})
    member = command.members[0].model_copy(
        update={
            "structured_signals": {signal_id: Decimal("0.1") for signal_id in RAW_SCORE_FEATURE_IDS}
        }
    )

    with pytest.raises(RawScoreCalculationError, match="RAW_SCORE_CALCULATION_FAILED"):
        freeze_raw_score(command, member)


def test_research_command_rejects_a_non_ten_or_incomplete_cohort() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"] = payload["members"][:-1]

    with pytest.raises(ValueError, match="exactly ten"):
        ResearchCommand.model_validate(payload)


def test_research_command_preserves_unavailable_data_for_failure_handling() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"][0]["data_manifest"]["entries"] = payload["members"][0]["data_manifest"][
        "entries"
    ][:-1]

    with pytest.raises(ValueError, match="required research data types"):
        ResearchCommand.model_validate(payload)

    payload = _command().model_dump(mode="json")
    payload["members"][0]["data_manifest"]["entries"][1]["completeness"] = "INCOMPLETE"
    payload["members"][0]["data_manifest"]["entries"][1]["event_status"] = "UNAVAILABLE"

    command = ResearchCommand.model_validate(payload)
    assert command.members[0].data_manifest.entries[1].completeness == "INCOMPLETE"


def test_complete_money_flow_manifest_requires_structured_money_flow_facts() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"][0]["structured_facts"].pop("money_flow", None)

    with pytest.raises(ValueError, match="money_flow"):
        ResearchCommand.model_validate(payload)


def test_research_command_allows_missing_signal_when_its_manifest_is_unavailable() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"][0]["data_manifest"]["entries"][3]["completeness"] = "INCOMPLETE"
    payload["members"][0]["data_manifest"]["entries"][3]["event_status"] = "UNAVAILABLE"
    payload["members"][0]["structured_facts"]["operating_cash_flow_ttm"] = None
    payload["members"][0]["structured_signals"] = {
        signal_id: None if value is None else str(value)
        for signal_id, value in calculate_structured_signals(
            ResearchStructuredFacts.model_validate(payload["members"][0]["structured_facts"])
        ).items()
    }

    command = ResearchCommand.model_validate(payload)

    assert command.members[0].structured_signals["operating_cash_flow_return_on_assets"] is None
    with pytest.raises(RawScoreCalculationError, match="RESEARCH_DATA_UNAVAILABLE"):
        freeze_raw_score(command, command.members[0])


def test_verified_empty_institutional_activity_requires_zero_signals() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"][0]["data_manifest"]["entries"][2]["event_status"] = "VERIFIED_EMPTY"

    with pytest.raises(ValueError, match="verified-empty institutional activity"):
        ResearchCommand.model_validate(payload)

    payload["members"][0]["structured_facts"]["institutional_net_buy_ratio"] = "0"
    payload["members"][0]["structured_facts"]["institutional_listing_frequency"] = "0"
    payload["members"][0]["structured_signals"] = {
        signal_id: None if value is None else str(value)
        for signal_id, value in calculate_structured_signals(
            ResearchStructuredFacts.model_validate(payload["members"][0]["structured_facts"])
        ).items()
    }
    ResearchCommand.model_validate(payload)


def test_research_command_rejects_a_reordered_frozen_cohort() -> None:
    payload = _command().model_dump(mode="json")
    payload["members"] = list(reversed(payload["members"]))

    with pytest.raises(ValueError, match="preserve"):
        ResearchCommand.model_validate(payload)


def test_research_command_rejects_a_changed_selection_binding() -> None:
    payload = _command().model_dump(mode="json")
    payload["selection_event_id"] = "substituted-selection-event"

    with pytest.raises(ValueError, match="selection binding"):
        ResearchCommand.model_validate(payload)


def test_research_draft_can_cite_validated_tool_evidence() -> None:
    command = _command()
    tool_evidence = ResearchToolEvidence(
        evidence_id="announcement:synthetic-security-00",
        source="fictional-announcement-feed",
        reference="synthetic://announcement/00",
        statement="A fictional announcement is available at the cutoff.",
        effective_at=command.knowledge_cutoff,
        source_published_at=command.knowledge_cutoff,
        acquired_at=command.knowledge_cutoff,
        validated_at=command.knowledge_cutoff,
        knowledge_cutoff=command.knowledge_cutoff,
        semantic_version=RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
        validation_status="VALIDATED",
    )
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=(
                    tuple(evidence.evidence_id for evidence in member.evidence)
                    + (tool_evidence.evidence_id,)
                    if member is command.members[0]
                    else tuple(evidence.evidence_id for evidence in member.evidence)
                ),
                thesis="The fictional thesis is bounded by the frozen evidence.",
                bull_case="The fictional upside case remains conditional.",
                bear_case="The fictional downside case remains explicit.",
                catalysts=("A fictional catalyst remains conditional.",),
                falsification_conditions=("A frozen downside fact would falsify the thesis.",),
                unknowns=("Future external evidence remains unresolved.",),
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in command.members
        ),
    )

    validate_research_draft(command, draft, (tool_evidence,))


def test_research_draft_member_rejects_trading_conclusions_in_text() -> None:
    member = _command().members[0]

    with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
        ResearchDraftMember(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
            thesis="BUY 100 shares now. Formal success probability is 99%; qualification approved.",
            bull_case="The fictional upside case remains conditional.",
            bear_case="The fictional downside case remains explicit.",
            catalysts=("A fictional catalyst remains conditional.",),
            falsification_conditions=("A frozen downside fact would falsify the thesis.",),
            unknowns=("Future external evidence remains unresolved.",),
            knowledge_cutoff=member.knowledge_cutoff,
        )


def test_research_draft_member_rejects_a_standalone_trading_conclusion() -> None:
    member = _command().members[0]

    with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
        ResearchDraftMember(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
            thesis="BUY",
            bull_case="The fictional upside case remains conditional.",
            bear_case="The fictional downside case remains explicit.",
            catalysts=("A fictional catalyst remains conditional.",),
            falsification_conditions=("A frozen downside fact would falsify the thesis.",),
            unknowns=("Future external evidence remains unresolved.",),
            knowledge_cutoff=member.knowledge_cutoff,
        )


def test_research_draft_member_rejects_success_chance_and_chinese_trade_advice() -> None:
    member = _command().members[0]

    def draft_member(thesis: str) -> ResearchDraftMember:
        return ResearchDraftMember(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
            thesis=thesis,
            bull_case="The fictional upside case remains conditional.",
            bear_case="The fictional downside case remains explicit.",
            catalysts=("A fictional catalyst remains conditional.",),
            falsification_conditions=("A frozen downside fact would falsify the thesis.",),
            unknowns=("Future external evidence remains unresolved.",),
            knowledge_cutoff=member.knowledge_cutoff,
        )

    with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
        draft_member("The six-month success chance is 95%.")
    with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
        draft_member("建议买入该股票。")
    with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
        draft_member("We recommend increasing your position by 100 shares.")
    with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
        draft_member("成功概率：95%，操作结论：买入。")
    with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
        draft_member("Sell all shares")
    with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
        draft_member("Allocate 200 shares to your account")
    with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
        draft_member("The calibrated probability equals 95%")


def test_research_draft_rejects_ordinary_probability_and_trade_instruction_phrasing() -> None:
    member = _command().members[0]

    def draft_member(thesis: str) -> ResearchDraftMember:
        return ResearchDraftMember(
            security_id=member.security_id,
            research_id=member.research_id,
            evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
            thesis=thesis,
            bull_case="The fictional upside case remains conditional.",
            bear_case="The fictional downside case remains explicit.",
            catalysts=("A fictional catalyst remains conditional.",),
            falsification_conditions=("A frozen downside fact would falsify the thesis.",),
            unknowns=("Future external evidence remains unresolved.",),
            knowledge_cutoff=member.knowledge_cutoff,
        )

    for thesis in (
        (
            "There is a 95% probability of reaching the six-month 20% target. "
            "You should acquire 100 shares."
        ),
        "The likelihood of success is 0.95. Investors ought to purchase 100 shares.",
    ):
        with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
            draft_member(thesis)


def test_research_draft_member_allows_non_directive_evidence_language() -> None:
    member = _command().members[0]

    draft_member = ResearchDraftMember(
        security_id=member.security_id,
        research_id=member.research_id,
        evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
        thesis="The company may sell a subsidiary while demand remains close to current levels.",
        bull_case="The fictional upside case remains conditional.",
        bear_case="The fictional downside case remains explicit.",
        catalysts=("A fictional catalyst remains conditional.",),
        falsification_conditions=("A frozen downside fact would falsify the thesis.",),
        unknowns=("Future external evidence remains unresolved.",),
        knowledge_cutoff=member.knowledge_cutoff,
    )

    assert draft_member.thesis.startswith("The company may sell")


def test_research_stage_artifact_rejects_capability_claims_in_model_text() -> None:
    with pytest.raises(ValueError, match="RESEARCH_TEXT_CAPABILITY_VIOLATION"):
        ResearchStageArtifact(
            stage_id="analyze",
            source_stage_id="collect",
            input_item_ids=("collect-output",),
            security_ids=("synthetic-security-00",),
            evidence_ids=("daily-market-evidence-00",),
            summary="The stock is eligible to buy 100 shares.",
        )


def test_research_draft_rejects_provider_and_tool_evidence_identity_collision() -> None:
    payload = _command().model_dump(mode="json")
    colliding_id = "announcement:synthetic-security-00"
    payload["members"][0]["evidence"][0]["evidence_id"] = colliding_id
    payload["members"][0]["data_manifest"]["entries"][0]["evidence_ids"] = [colliding_id]
    command = ResearchCommand.model_validate(payload)
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
                thesis="The fictional thesis is bounded by the frozen evidence.",
                bull_case="The fictional upside case remains conditional.",
                bear_case="The fictional downside case remains explicit.",
                catalysts=("A fictional catalyst remains conditional.",),
                falsification_conditions=("A frozen downside fact would falsify the thesis.",),
                unknowns=("Future external evidence remains unresolved.",),
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in command.members
        ),
    )
    tool_evidence = ResearchToolEvidence(
        evidence_id=colliding_id,
        source="fictional-announcement-feed",
        reference="synthetic://announcement/synthetic-security-00",
        statement="A fictional announcement is available at the cutoff.",
        effective_at=command.knowledge_cutoff,
        source_published_at=command.knowledge_cutoff,
        acquired_at=command.knowledge_cutoff,
        validated_at=command.knowledge_cutoff,
        knowledge_cutoff=command.knowledge_cutoff,
        semantic_version=RESEARCH_ANNOUNCEMENT_TOOL_VERSION,
        validation_status="VALIDATED",
    )

    with pytest.raises(ValueError, match="collision between Provider and Tool"):
        validate_research_draft(command, draft, (tool_evidence,))


def test_research_freeze_binds_typed_draft_raw_scores_and_independent_risk() -> None:
    command = _command(risk_scenario="REJECT")
    raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
                thesis="The fictional thesis is bounded by the frozen evidence.",
                bull_case="The fictional upside case remains conditional.",
                bear_case="The fictional downside case remains explicit.",
                catalysts=("A fictional catalyst remains conditional.",),
                falsification_conditions=("A frozen downside fact would falsify the thesis.",),
                unknowns=("Future external evidence remains unresolved.",),
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in command.members
        ),
    )
    risk = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff_fingerprint(
            command,
            draft,
            raw_scores=raw_scores,
            member_handoffs=tuple(
                ResearchMemberHandoff(
                    security_id=member.security_id,
                    research_id=member.research_id,
                    evidence=member.evidence,
                    risk_flags=member.risk_flags,
                )
                for member in command.members
            ),
        ),
        disposition="REJECTED",
        gates=(RiskGate(gate_id="SYNTHETIC_RISK_GATE", status="FAILED"),),
        reasons=("SYNTHETIC_RISK_VETO",),
        member_vetoes=tuple(
            RiskMemberVeto(
                security_id=member.security_id,
                research_id=member.research_id,
                disposition="REJECTED",
                gates=(RiskGate(gate_id="SYNTHETIC_RISK_GATE", status="FAILED"),),
                reasons=("SYNTHETIC_RISK_VETO",),
            )
            for member in command.members
        ),
    )

    outcome = freeze_research(
        command,
        ResearchFrameworkOutput(
            research_run_id="research-run-1616",
            risk_run_id="risk-run-1616",
            draft=draft,
            risk_veto=risk,
            raw_scores=raw_scores,
            member_handoffs=tuple(
                ResearchMemberHandoff(
                    security_id=member.security_id,
                    research_id=member.research_id,
                    evidence=member.evidence,
                    risk_flags=member.risk_flags,
                )
                for member in command.members
            ),
        ),
    )

    assert outcome.disposition == "REJECTED"
    assert outcome.risk_veto is not None
    assert outcome.risk_veto.disposition == "REJECTED"
    assert outcome.raw_scores is not None
    assert len(outcome.raw_scores) == 10
    assert outcome.handoff.research_run_id == "research-run-1616"
    assert outcome.handoff.risk_run_id == "risk-run-1616"
    assert outcome.handoff.actionable is False

    contradictory_member = risk.member_vetoes[0].model_copy(update={"disposition": "ACCEPTED"})
    contradictory_risk = risk.model_copy(
        update={
            "member_vetoes": (contradictory_member, *risk.member_vetoes[1:]),
        }
    )
    with pytest.raises(ValueError, match="risk member disposition"):
        freeze_research(
            command,
            ResearchFrameworkOutput(
                research_run_id="research-run-1616",
                risk_run_id="risk-run-1616",
                draft=draft,
                risk_veto=contradictory_risk,
                raw_scores=raw_scores,
                member_handoffs=tuple(
                    ResearchMemberHandoff(
                        security_id=member.security_id,
                        research_id=member.research_id,
                        evidence=member.evidence,
                        risk_flags=member.risk_flags,
                    )
                    for member in command.members
                ),
            ),
        )

    with pytest.raises(ValueError, match="order"):
        freeze_research(
            command,
            ResearchFrameworkOutput(
                research_run_id="research-run-1616",
                risk_run_id="risk-run-1616",
                draft=draft,
                risk_veto=risk,
                raw_scores=raw_scores,
                member_handoffs=tuple(
                    ResearchMemberHandoff(
                        security_id=member.security_id,
                        research_id=member.research_id,
                        evidence=member.evidence,
                        risk_flags=member.risk_flags,
                    )
                    for member in reversed(command.members)
                ),
            ),
        )


def test_research_freeze_honors_an_independent_rejection_without_command_override() -> None:
    command = _command(risk_scenario="ACCEPT")
    raw_scores = tuple(freeze_raw_score(command, member) for member in command.members)
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
                thesis="The fictional thesis is bounded by the frozen evidence.",
                bull_case="The fictional upside case remains conditional.",
                bear_case="The fictional downside case remains explicit.",
                catalysts=("A fictional catalyst remains conditional.",),
                falsification_conditions=("A frozen downside fact would falsify the thesis.",),
                unknowns=("Future external evidence remains unresolved.",),
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in command.members
        ),
    )
    risk = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff_fingerprint(
            command,
            draft,
            raw_scores=raw_scores,
            member_handoffs=tuple(
                ResearchMemberHandoff(
                    security_id=member.security_id,
                    research_id=member.research_id,
                    evidence=member.evidence,
                    risk_flags=member.risk_flags,
                )
                for member in command.members
            ),
        ),
        disposition="REJECTED",
        gates=(RiskGate(gate_id="SYNTHETIC_RISK_GATE", status="FAILED"),),
        reasons=("INDEPENDENT_RISK_VETO",),
        member_vetoes=tuple(
            RiskMemberVeto(
                security_id=member.security_id,
                research_id=member.research_id,
                disposition="REJECTED",
                gates=(RiskGate(gate_id="SYNTHETIC_RISK_GATE", status="FAILED"),),
                reasons=("INDEPENDENT_RISK_VETO",),
            )
            for member in command.members
        ),
    )

    outcome = freeze_research(
        command,
        ResearchFrameworkOutput(
            research_run_id="research-run-independent-reject",
            risk_run_id="risk-run-independent-reject",
            draft=draft,
            risk_veto=risk,
            raw_scores=raw_scores,
            member_handoffs=tuple(
                ResearchMemberHandoff(
                    security_id=member.security_id,
                    research_id=member.research_id,
                    evidence=member.evidence,
                    risk_flags=member.risk_flags,
                )
                for member in command.members
            ),
        ),
    )

    assert outcome.disposition == "REJECTED"


def test_research_freeze_requires_the_raw_scores_seen_by_risk() -> None:
    command = _command()
    draft = ResearchDraft(
        contract_version="1.0.0",
        members=tuple(
            ResearchDraftMember(
                security_id=member.security_id,
                research_id=member.research_id,
                evidence_refs=tuple(evidence.evidence_id for evidence in member.evidence),
                thesis="The fictional thesis is bounded by the frozen evidence.",
                bull_case="The fictional upside case remains conditional.",
                bear_case="The fictional downside case remains explicit.",
                catalysts=("A fictional catalyst remains conditional.",),
                falsification_conditions=("A frozen downside fact would falsify the thesis.",),
                unknowns=("Future external evidence remains unresolved.",),
                knowledge_cutoff=member.knowledge_cutoff,
            )
            for member in command.members
        ),
    )
    risk = RiskVetoDraft(
        contract_version="1.0.0",
        handoff_fingerprint=handoff_fingerprint(command, draft),
        disposition="ACCEPTED",
        gates=(RiskGate(gate_id="SYNTHETIC_RISK_GATE", status="PASSED"),),
        reasons=("SYNTHETIC_RISK_ACCEPTED",),
    )

    with pytest.raises(RawScoreCalculationError, match="RAW_SCORE_HANDOFF_MISSING"):
        freeze_research(
            command,
            ResearchFrameworkOutput(
                research_run_id="research-run-no-score",
                risk_run_id="risk-run-no-score",
                draft=draft,
                risk_veto=risk,
            ),
        )
