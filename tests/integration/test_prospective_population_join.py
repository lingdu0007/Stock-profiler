"""Original D0 host facts reach only saved SHADOW population aggregates."""

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from synthetic_candidate_workspace import failed_candidate_case
from test_prospective_shadow_cycle import cycle_case, observation_case, review_case, run_case, saved
from test_research_risk_veto import _case
from test_scoped_qualification import GovernanceClock, case_payload
from test_standard_outcomes import bind_outcome_input, standard_observation

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.candidate_selection.calibrated_candidates import (
    CandidateReleaseCommand,
    CandidateReleaseOutcome,
)
from stock_profiler.modules.decision_cases.domain import (
    DecisionEventFact,
    ExternalResult,
    FrozenDecisionCase,
)
from stock_profiler.modules.prospective.contracts import CycleCommand
from stock_profiler.modules.research.contracts import selection_binding_sha256


def store(
    settings: Settings, case: FrozenDecisionCase, result: ExternalResult, *, persist: bool = True
) -> DecisionEventFact:
    ledger = DecisionLedger.from_settings(settings, clock=GovernanceClock(case.report_generated_at))
    if not persist:
        # Uncommitted blueprints supply future policy versions, never population evidence.
        return ledger.build_event_fact(
            case=case,
            framework_run_id=case.framework_run_id,
            result=result,
            stage_results=(),
        )
    ledger.persist_business_mapping_before_framework(case)
    with ledger.serialize_case_execution() as connection:
        return ledger.commit_event(
            connection,
            case=case,
            framework_run_id=case.framework_run_id,
            result=result,
            stage_results=(),
        )


def sources(
    settings: Settings, *, selection_abstained: bool = False, persist: bool = True
) -> tuple[DecisionEventFact, DecisionEventFact, DecisionEventFact]:
    """Save fixture results through the host, without fitting a candidate model."""
    original = _case(settings, risk_scenario="ACCEPT", user_id="stock-profiler-single-user")
    assert original.research is not None
    ledger = DecisionLedger.from_settings(settings)
    with ledger.serialize_case_execution() as connection:
        selection = ledger.get_decision_event(original.research.selection_event_id, connection)
    assert selection is not None and selection.result.selection is not None
    payload = json.loads(
        json.dumps(selection.case.model_dump(mode="json")).replace(
            "2042-06-30T23:59:59", "2042-06-30T15:59:59"
        )
    )
    payload["case_id"] += "-shadow"
    payload["business_identity"] += "-shadow"
    payload["access_scope"]["visibility"] = "SHADOW"
    selection_case = FrozenDecisionCase.model_validate(payload)
    selected_result = selection.result.selection
    if selection_abstained:
        selected_result = selected_result.model_copy(
            update={
                "disposition": "ABSTAINED",
                "members": (),
                "population": selected_result.population.model_copy(
                    update={"selection_pass": False}
                ),
            }
        )
    selection = store(
        settings,
        selection_case,
        selection.result.model_copy(
            update={
                "selection": selected_result.model_copy(
                    update={
                        "cutoff_at": datetime.fromisoformat(selection_case.knowledge_cutoff),
                    }
                ),
                "evaluation_registrations": (),
            }
        ),
        persist=persist,
    )
    payload = json.loads(
        json.dumps(original.model_dump(mode="json")).replace(
            "2042-06-30T23:59:59", "2042-06-30T15:59:59"
        )
    )
    payload["case_id"] += "-shadow"
    payload["business_identity"] += "-shadow"
    payload["access_scope"]["visibility"] = "SHADOW"
    assert selection.case.selection is not None
    command = original.research.model_copy(
        update={
            "cutoff_at": selection.case.selection.cutoff_at,
            "knowledge_cutoff": selection.case.selection.cutoff_at,
            "selection_object_id": selection.business_object_id,
            "selection_event_id": selection.decision_event_id,
        }
    )
    payload["research"].update(
        selection_object_id=selection.business_object_id,
        selection_event_id=selection.decision_event_id,
        selection_fingerprint=selection_binding_sha256(
            selection.business_object_id,
            selection.decision_event_id,
            command.cutoff_at,
            command.screening,
        ),
    )
    payload["input"]["research"] = payload["research"]
    research_case = FrozenDecisionCase.model_validate(payload)
    research = store(
        settings,
        research_case,
        original.expected_external_result,
        persist=persist and not selection_abstained,
    )
    payload = dict[str, Any](failed_candidate_case(settings))
    payload["case_id"] = "fictional-shadow-source-release"
    payload["business_identity"] = "fictional-shadow-source-release"
    payload["access_scope"]["visibility"] = "SHADOW"
    payload.update(
        knowledge_cutoff=selection.case.knowledge_cutoff,
        report_generated_at="2042-07-07T15:00:00Z",
    )
    payload["candidate_release"].update(
        knowledge_cutoff=payload["knowledge_cutoff"],
        published_at=payload["report_generated_at"],
        label_watermark_at=payload["knowledge_cutoff"],
        research_event_id=research.decision_event_id,
        research_object_id=research.business_object_id,
        market_calendar_version="synthetic-market-calendar-v1",
        capability_version="fictional-t1-v1",
    )
    candidate_input = payload["candidate_release"]["candidates"][0]
    payload["candidate_release"]["candidates"] = [
        dict(
            candidate_input,
            security_id=f"synthetic-security-{index:02}",
            research_id=f"research-{index:02}",
        )
        for index in range(10)
    ]
    payload["candidate_release"] = CandidateReleaseCommand.model_validate(
        payload["candidate_release"]
    ).model_dump(mode="json")
    payload["input"]["candidate_release"] = payload["candidate_release"]
    release_case = FrozenDecisionCase.model_validate(payload)
    release = json.loads(
        (Path(__file__).parents[1] / "fixtures/synthetic/candidate_workspace.json").read_text()
    )["workspace"]["releases"][0]["release"]
    release.update(
        knowledge_cutoff=payload["knowledge_cutoff"],
        published_at=payload["report_generated_at"],
        research_event_id=research.decision_event_id,
        research_object_id=research.business_object_id,
        market_calendar_version="synthetic-market-calendar-v1",
        capability_version="fictional-t1-v1",
    )
    template = release["members"][0]
    release["members"] = []
    for index in range(10):
        member = deepcopy(template)
        member.update(
            security_id=f"synthetic-security-{index:02}",
            research_id=f"research-{index:02}",
            calibrated_probability="0.85",
            raw_success_score="0",
            candidate=False,
        )
        release["members"].append(member)
    release.update(disposition="VALID_NO_CANDIDATES")
    release["population"].update(valid_monthly=True, recommendation_coverage_pass=False)
    months = [f"{2037 + index // 12}-{index % 12 + 1:02}" for index in range(60)]
    diagnostics = {
        "log_loss": "0.2",
        "brier_score": "0.1",
        "reliability_curve": [
            {
                "lower_probability": "0.8",
                "upper_probability": "1",
                "sample_count": 600,
                "mean_predicted_probability": "0.85",
                "observed_success_rate": "0.8",
            }
        ],
        "recalibration_fit_status": "AVAILABLE",
        "calibration_intercept": "0",
        "calibration_slope": "1",
    }
    release["calibration"] = {
        "calibrator_version": "monotone-firth-logistic-v1",
        "intercept": "1.7346010553881064",
        "slope": "1",
        "calibrator_selection": {
            "policy": "PRE_REGISTERED_V1_SINGLE_FAMILY",
            "selected_calibrator_version": "monotone-firth-logistic-v1",
            "window_months": months[:24],
            "label_watermark_at": "2042-01-01T00:00:00Z",
            "record_count": 600,
            "diagnostics": diagnostics,
        },
        "calibrator_selection_window_months": months[:24],
        "training_window_months": months,
        "label_watermark_at": "2042-01-01T00:00:00Z",
        "training_record_count": 600,
        "positive_record_count": 480,
        "negative_record_count": 120,
        "out_of_sample_diagnostics": diagnostics,
        "recent_diagnostic_months": months[-24:],
        "recent_diagnostic_sample_count": 240,
        "recent_diagnostic_status": "AVAILABLE",
        "recent_diagnostics": diagnostics,
    }
    candidate = store(
        settings,
        release_case,
        ExternalResult(
            outcome_code="FICTIONAL_SHADOW_SOURCE",
            summary="Original synthetic snapshot.",
            key_reasons=("SYNTHETIC",),
            candidate_release=CandidateReleaseOutcome.model_validate(release),
        ),
        persist=persist and not selection_abstained,
    )
    return selection, research, candidate


def registration_payload(
    settings: Settings,
    facts: tuple[DecisionEventFact, DecisionEventFact, DecisionEventFact],
    *,
    score_version: str | None = None,
    formal: bool = False,
) -> dict[str, Any]:
    selection, research, candidate = facts
    assert selection.case.selection is not None
    assert research.case.research is not None
    assert candidate.case.candidate_release is not None
    payload = cycle_case(settings)
    payload["prospective"]["registration"]["calendar_version"] = "synthetic-market-calendar-v1"
    payload["prospective"]["registration"]["plan_nodes"][0].update(
        first_entry_at="2042-07-01T08:00:00Z",
        disclosure_at="2042-07-07T15:00:00Z",
        matures_at="2043-01-07T15:00:00Z",
    )
    bundle = deepcopy(payload["version_bundle"])
    for name in (
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    ):
        bundle[name] = "standard-outcomes.1.0.0"
    payload["prospective"]["registration"]["population_policy"] = {
        "maturity": {
            "observation_batches": 12,
            "observation_windows": 2,
            "high_band_threshold": "0.80",
        },
        "cohort_size": 10,
        "selection_policy": selection.case.selection.policy.model_dump(mode="json"),
        "selection_strategy_version": selection.case.selection.strategy_version,
        "raw_score_model_version": score_version
        or research.case.research.raw_score_model.model_version,
        "calibrator_version": candidate.case.candidate_release.calibrator_version,
        "standard_quantity": "100",
        "selection_bundle": selection.case.version_bundle.model_dump(mode="json"),
        "research_bundle": research.case.version_bundle.model_dump(mode="json"),
        "candidate_bundle": candidate.case.version_bundle.model_dump(mode="json"),
        "standard_bundle": bundle,
    }
    if formal:
        population = payload["prospective"]["registration"]["population_policy"]
        payload["prospective"]["registration"]["formal_policy"] = {
            "cohort": {
                "version_id": "fictional-prospective-baselines-v1",
                "source_version_bundle": population["selection_bundle"],
                "strategy_version": population["selection_strategy_version"],
                "market_calendar_version": "synthetic-market-calendar-v1",
                "standard_quantity": "100",
                "selection_policy": population["selection_policy"],
                "positive_members_required": 8,
                "target_members_required": 5,
                "terminal_target": ".20",
                "maximum_drawdown": ".20",
                "random_trials": 10000,
            },
            "node_months": ["2042-06"],
            "increment_batches": 6,
            "increment_windows": 1,
            "total_alpha": ".05",
            "block_lengths": [6, 9, 12],
            "bootstrap_repetitions": 199,
            "inner_repetitions": 49,
            "overall_pass_floor": ".8",
            "overall_drawdown_floor": ".8",
            "calibration_success_floor": ".8",
            "overconfidence_ceiling": ".05",
            "minimum_availability": ".95",
            "coverage_floor": ".50",
            "regime_pass_floor": ".5",
            "regime_drawdown_floor": ".6",
            "regime_batches": 30,
            "regime_formed": 24,
            "regime_windows": 5,
            "regime_periods": 2,
            "regime_high_band_records": 100,
        }
    payload["prospective"] = CycleCommand.model_validate(payload["prospective"]).model_dump(
        mode="json"
    )
    payload["input"]["prospective"] = payload["prospective"]
    return payload


def registered_cycle(
    settings: Settings,
    facts: tuple[DecisionEventFact, DecisionEventFact, DecisionEventFact],
    *,
    score_version: str | None = None,
    formal: bool = False,
) -> DecisionEventFact:
    return saved(
        settings,
        run_case(
            settings,
            registration_payload(
                settings,
                facts,
                score_version=score_version,
                formal=formal,
            ),
        ),
    )


def linked_observation(
    settings: Settings, registered: str, selection: str, candidate: str
) -> dict[str, Any]:
    payload = observation_case(settings, registered)
    cutoff = "2042-07-07T15:00:00Z"
    payload.update(knowledge_cutoff=cutoff, report_generated_at=cutoff)
    payload["prospective"]["cutoff_at"] = cutoff
    payload["prospective"]["observation"].update(
        completed_at=cutoff,
        batch_saved_at=cutoff,
        report_saved_at=cutoff,
    )
    payload["prospective"]["observation"]["batch_link"] = {
        "selection_event_id": selection,
        "candidate_event_id": candidate,
    }
    return payload


def test_saved_source_links_form_a_pending_then_due_missing_queue(
    migrated_settings: Settings,
) -> None:
    facts = sources(migrated_settings)
    registered = registered_cycle(migrated_settings, facts)
    observed = saved(
        migrated_settings,
        run_case(
            migrated_settings,
            linked_observation(
                migrated_settings,
                registered.decision_event_id,
                facts[0].decision_event_id,
                facts[2].decision_event_id,
            ),
        ),
    )
    assert observed.result.prospective is not None
    assert observed.result.prospective.watermark is not None
    assert observed.result.prospective.watermark.pending_batches == 1
    reviewed = saved(
        migrated_settings,
        run_case(
            migrated_settings,
            review_case(
                migrated_settings,
                registered.decision_event_id,
                observed.decision_event_id,
            ),
        ),
    )
    assert reviewed.result.prospective is not None
    assert reviewed.result.prospective.watermark is not None
    assert reviewed.result.prospective.watermark.due_missing_batches == 1
    assert reviewed.result.prospective.watermark.mature_batches == 0
    assert (
        saved(
            migrated_settings,
            run_case(
                migrated_settings,
                linked_observation(
                    migrated_settings,
                    registered.decision_event_id,
                    facts[0].decision_event_id,
                    facts[2].decision_event_id,
                ),
            ),
        )
        == observed
    )


def test_missing_saved_source_is_rejected_before_a_population_is_registered(
    migrated_settings: Settings,
) -> None:
    facts = sources(migrated_settings)
    registered = registered_cycle(migrated_settings, facts)
    payload = linked_observation(
        migrated_settings,
        registered.decision_event_id,
        "missing-original-selection",
        facts[2].decision_event_id,
    )
    with pytest.raises(ValueError, match="PROSPECTIVE_BATCH_SOURCE_UNAVAILABLE"):
        run_case(migrated_settings, payload)


def test_another_owner_source_cannot_enter_the_saved_shadow_population(
    migrated_settings: Settings,
) -> None:
    facts = sources(migrated_settings)
    registered = registered_cycle(migrated_settings, facts)
    original = facts[2]
    assert original.case.access_scope is not None
    other = original.case.model_copy(
        update={
            "case_id": "fictional-other-source",
            "business_identity": "fictional-other-source",
            "access_scope": original.case.access_scope.model_copy(
                update={"user_id": "fictional-other-owner"}
            ),
        }
    )
    outsider = store(
        migrated_settings,
        other,
        original.result.model_copy(update={"evaluation_registrations": ()}),
    )
    payload = linked_observation(
        migrated_settings,
        registered.decision_event_id,
        facts[0].decision_event_id,
        outsider.decision_event_id,
    )
    with pytest.raises(ValueError, match="PROSPECTIVE_BATCH_SOURCE_UNAVAILABLE"):
        run_case(migrated_settings, payload)


def test_saved_model_must_match_the_original_preregistered_model_lock(
    migrated_settings: Settings,
) -> None:
    facts = sources(migrated_settings)
    registered = registered_cycle(
        migrated_settings, facts, score_version="fictional-unrelated-score-version"
    )
    payload = linked_observation(
        migrated_settings,
        registered.decision_event_id,
        facts[0].decision_event_id,
        facts[2].decision_event_id,
    )
    with pytest.raises(ValueError, match="PROSPECTIVE_BATCH_MODEL_LOCK_MISMATCH"):
        run_case(migrated_settings, payload)


def test_renaming_a_candidate_cannot_replace_its_original_monthly_host_identity(
    migrated_settings: Settings,
) -> None:
    facts = sources(migrated_settings)
    original = facts[2]
    retry = original.case.model_dump(mode="json")
    retry.update(
        case_id="fictional-retried-candidate", business_identity="fictional-retried-candidate"
    )
    retry["candidate_release"]["batch_id"] = "fictional-retried-monthly-batch"
    retry["input"]["candidate_release"] = retry["candidate_release"]
    execution = run_case(migrated_settings, retry)
    assert execution.decision_event_id == original.decision_event_id
    assert saved(migrated_settings, execution) == original
    ledger = DecisionLedger.from_settings(migrated_settings)
    assert original.case.access_scope is not None
    with ledger.serialize_case_execution() as connection:
        history = ledger.candidate_release_history(connection, original.case.access_scope)
    assert tuple(event.decision_event_id for event in history) == (original.decision_event_id,)


def test_complete_saved_standard_population_advances_maturity_without_alpha(
    migrated_settings: Settings,
) -> None:
    facts = sources(migrated_settings)
    registered = registered_cycle(migrated_settings, facts)
    observed = saved(
        migrated_settings,
        run_case(
            migrated_settings,
            linked_observation(
                migrated_settings,
                registered.decision_event_id,
                facts[0].decision_event_id,
                facts[2].decision_event_id,
            ),
        ),
    )
    payload = case_payload(
        migrated_settings, "shadow-standard", {}, contract_version="standard-outcomes.1.0.0"
    )
    payload.pop("governance")
    payload["access_scope"]["visibility"] = "SHADOW"
    cutoff = "2043-02-01T00:00:00Z"
    payload.update(knowledge_cutoff=cutoff, report_generated_at=cutoff)
    observations = []
    for index in range(10):
        item = standard_observation()
        item["security_id"] = f"synthetic-security-{index:02}"
        item["entry"]["evidence_id"] = f"fictional-entry-{index:02}"
        item["terminal"]["evidence_id"] = f"fictional-terminal-{index:02}"
        observations.append(item)
    payload["standard_outcomes"] = {
        "contract_version": "1.0.0",
        "synthetic": True,
        "generator_version": "fictional-shadow-standard/1",
        "seed": 2291,
        "selection_event_id": facts[0].decision_event_id,
        "cutoff_at": cutoff,
        "market_calendar_version": "synthetic-market-calendar-v1",
        "standard_quantity": "100",
        "observations": observations,
        "previous_event_id": None,
    }
    bind_outcome_input(payload)
    standard = saved(migrated_settings, run_case(migrated_settings, payload))
    assert standard.result.standard_outcomes is not None
    assert standard.result.standard_outcomes.populations["SELECTION"].evaluable == 10
    reviewed = saved(
        migrated_settings,
        run_case(
            migrated_settings,
            review_case(
                migrated_settings,
                registered.decision_event_id,
                observed.decision_event_id,
            ),
        ),
    )
    assert reviewed.result.prospective is not None
    assert reviewed.result.prospective.watermark is not None
    assert reviewed.result.prospective.watermark.mature_batches == 1
    assert reviewed.result.prospective.watermark.high_band_records == 10
    assert reviewed.result.prospective.watermark.due_missing_batches == 0
    assert reviewed.result.prospective.formal_look is not None
    assert reviewed.result.prospective.formal_look.actual_ordinal == 0
    assert reviewed.result.prospective.formal_look.alpha_spent == 0
    assert observed.result.prospective is not None
    assert observed.result.prospective.watermark is not None
    assert observed.result.prospective.watermark.mature_batches == 0


def test_original_selection_abstention_requires_no_downstream_population(
    migrated_settings: Settings,
) -> None:
    facts = sources(migrated_settings, selection_abstained=True)
    registered = registered_cycle(migrated_settings, facts)
    payload = linked_observation(
        migrated_settings,
        registered.decision_event_id,
        facts[0].decision_event_id,
        facts[2].decision_event_id,
    )
    payload["prospective"]["observation"]["batch_link"].pop("candidate_event_id")
    payload["prospective"]["observation"]["batch_saved_at"] = facts[0].committed_at
    observed = saved(migrated_settings, run_case(migrated_settings, payload))
    reviewed = saved(
        migrated_settings,
        run_case(
            migrated_settings,
            review_case(
                migrated_settings,
                registered.decision_event_id,
                observed.decision_event_id,
            ),
        ),
    )
    assert reviewed.result.prospective is not None
    assert reviewed.result.prospective.watermark is not None
    assert reviewed.result.prospective.watermark.mature_batches == 1
    assert reviewed.result.prospective.watermark.high_band_records == 0
    assert reviewed.result.prospective.watermark.due_missing_batches == 0
    ledger = DecisionLedger.from_settings(migrated_settings)
    assert facts[0].case.access_scope is not None
    with ledger.serialize_case_execution() as connection:
        assert ledger.candidate_release_history(connection, facts[0].case.access_scope) == ()
        assert ledger.get_decision_event(facts[1].decision_event_id, connection) is None
        assert ledger.get_decision_event(facts[2].decision_event_id, connection) is None
