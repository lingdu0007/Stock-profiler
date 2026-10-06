"""Historical probability evidence through the frozen-case and saved-report seam."""

from typing import Any

from test_scoped_qualification import GovernanceClock, case_payload
from test_selection_cohort import selection_payload

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings


def registration_case(settings: Settings) -> dict[str, Any]:
    payload = case_payload(
        settings, "probability-registration", {}, contract_version="historical-probability.1.0.0"
    )
    payload.pop("governance")
    cutoff = "2033-01-01T00:00:00Z"
    payload.update(knowledge_cutoff=cutoff, report_generated_at=cutoff)
    source_versions = dict(payload["version_bundle"])
    for key in (
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    ):
        source_versions[key] = "candidate-release.1.0.0"
    payload["historical_probability"] = {
        "operation": "REGISTER",
        "synthetic": True,
        "generator_version": "fictional-probability-history/1",
        "seed": 2121,
        "cutoff_at": cutoff,
        "registration_event_id": None,
        "previous_event_id": None,
        "indices": [],
        "registration": {
            "version_id": "fictional-probability-history-v1",
            "start_month": "2042-06",
            "end_month": "2042-06",
            "source_version_bundle": source_versions,
            "capability_version": "fictional-candidate-v1",
            "raw_score_model_version": "elastic-net-logistic-z20-v1",
            "selection_strategy_version": "synthetic-dual-head-strategy-v1",
            "minimum_availability": "0.95",
            "market_calendar_version": "synthetic-market-calendar-v1",
            "calibrator_version": "monotone-firth-logistic-v1",
            "high_band_threshold": "0.8",
            "success_floor": "0.8",
            "overconfidence_ceiling": "0.05",
            "coverage_floor": "0.5",
            "overall_months": 120,
            "overall_high_band_records": 500,
            "overall_nonoverlapping_windows": 20,
            "regime_months": 30,
            "regime_high_band_records": 100,
            "regime_nonoverlapping_windows": 5,
            "regime_periods": 2,
            "block_lengths": [6, 9, 12],
            "confidence": "0.95",
            "bootstrap_repetitions": 199,
        },
    }
    payload["input"]["historical_probability"] = payload["historical_probability"]
    return payload


def test_preregistration_is_immutable_and_never_grants_authority(
    migrated_settings: Settings,
) -> None:
    payload = registration_case(migrated_settings)
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None
    evidence = run.report.result.model_dump(mode="json")["historical_probability"]
    assert evidence["disposition"] == "REGISTERED"
    assert evidence["actionable"] is False
    assert evidence["authorization_granted"] is False
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
        ).report
        == run.report
    )


def evaluation_case(settings: Settings, registration_event_id: str) -> dict[str, Any]:
    payload = registration_case(settings)
    cutoff = "2043-03-01T00:00:00Z"
    payload.update(
        case_id="probability-evaluation",
        business_identity="probability-evaluation",
        knowledge_cutoff=cutoff,
        report_generated_at=cutoff,
    )
    payload["historical_probability"].update(
        operation="EVALUATE",
        registration=None,
        registration_event_id=registration_event_id,
        cutoff_at=cutoff,
    )
    payload["input"]["historical_probability"] = payload["historical_probability"]
    return payload


def test_missing_planned_month_remains_indeterminate(migrated_settings: Settings) -> None:
    registration = registration_case(migrated_settings)
    registered = run_frozen_decision_case(
        migrated_settings, registration, clock=GovernanceClock(registration["knowledge_cutoff"])
    )
    assert registered.report is not None
    payload = evaluation_case(migrated_settings, registered.report.event_id)
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None
    evidence = run.report.result.model_dump(mode="json")["historical_probability"]
    assert evidence["disposition"] == "INDETERMINATE"
    assert evidence["counts"]["planned"] == 1
    assert evidence["counts"]["missing_months"] == 1
    assert evidence["months"][0]["plan_month"] == "2042-06"
    assert evidence["months"][0]["disposition"] == "MISSING"


def seed_probability_source(settings: Settings) -> str:
    """Generate original frozen probability facts behind the ledger adapter."""
    from datetime import datetime
    from decimal import Decimal

    from synthetic_candidate_workspace import failed_candidate_case
    from synthetic_probability_source import command as calibrator_case

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.candidate_selection.calibrated_candidates import (
        freeze_candidate_release,
    )
    from stock_profiler.modules.decision_cases.domain import (
        ExternalResult,
        FrozenDecisionCase,
        ResultAccessScope,
    )

    payload: dict[str, Any] = dict(failed_candidate_case(settings))
    payload.update(
        knowledge_cutoff="2042-06-30T23:59:59Z", report_generated_at="2042-07-01T01:00:00Z"
    )
    ledger = DecisionLedger.from_settings(
        settings, clock=GovernanceClock(payload["report_generated_at"])
    )
    with ledger.serialize_case_execution() as connection:
        research = ledger.research_event_history(
            connection, ResultAccessScope.model_validate(payload["access_scope"])
        )[0]
    assert research.case.research is not None
    raw_model_version = research.case.research.raw_score_model.model_version
    original = calibrator_case()
    from stock_profiler.modules.portfolio.market_calendar import six_month_terminal_evaluation_at

    def shift(record: Any) -> Any:
        entry = record.entry_at.replace(year=record.entry_at.year - 4)
        frozen = record.raw_score_frozen_at.replace(year=record.raw_score_frozen_at.year - 4)
        maturity = six_month_terminal_evaluation_at(entry, record.market_calendar_version)
        return record.model_copy(
            update={
                "month": frozen.strftime("%Y-%m"),
                "raw_score_model_version": raw_model_version,
                "entry_at": entry,
                "raw_score_frozen_at": frozen,
                "raw_score_training_watermark_at": record.raw_score_training_watermark_at.replace(
                    year=record.raw_score_training_watermark_at.year - 4
                ),
                "entry_window_ends_at": record.entry_window_ends_at.replace(
                    year=record.entry_window_ends_at.year - 4
                ),
                "unified_maturity_at": maturity,
                "label_available_at": maturity,
            }
        )

    for records_field, months_field in (
        ("training_records", "training_window_months"),
        ("calibrator_selection_records", "calibrator_selection_window_months"),
        ("recent_diagnostic_records", "recent_diagnostic_window_months"),
    ):
        records = tuple(shift(record) for record in getattr(original, records_field))
        original = original.model_copy(
            update={
                records_field: records,
                months_field: tuple(dict.fromkeys(record.month for record in records)),
            }
        )
    original = original.model_copy(
        update={
            "knowledge_cutoff": datetime.fromisoformat(payload["knowledge_cutoff"]),
            "published_at": datetime.fromisoformat(payload["report_generated_at"]),
            "market_calendar_version": "synthetic-market-calendar-v1",
            "research_event_id": research.decision_event_id,
            "research_object_id": research.business_object_id,
            "candidates": tuple(
                original.candidates[0].model_copy(
                    update={
                        "security_id": f"synthetic-security-{i:02}",
                        "research_id": f"fictional-research-{i:02}",
                        "raw_success_score": Decimal("-0.95") if i == 1 else Decimal("0.95"),
                        "risk_status": "REJECTED" if i == 2 else "ACCEPTED",
                    }
                )
                for i in range(10)
            ),
            "market_sessions": (),
            "qualifications": (),
            "label_watermark_at": datetime.fromisoformat(payload["knowledge_cutoff"]),
        }
    )
    # The saved calendar supplies the first five actually available sessions.
    from stock_profiler.modules.candidate_selection.calibrated_candidates import MarketSession
    from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar

    calendar = synthetic_market_calendar(original.market_calendar_version)
    assert calendar is not None
    sessions = calendar.sessions_after_open(original.knowledge_cutoff, 5)
    assert len(sessions) == 5
    original = original.model_copy(
        update={
            "market_sessions": tuple(
                MarketSession(
                    market_date=s.closed_at.date(),
                    opens_at=s.closed_at.replace(hour=8),
                    closes_at=s.closed_at,
                    session_sequence=s.ordinal,
                )
                for s in sessions
            ),
            "last_completed_market_session_sequence": sessions[0].ordinal - 1,
        }
    )
    outcome = freeze_candidate_release(original)
    assert outcome.calibration is not None
    payload["candidate_release"] = original.model_dump(mode="json")
    payload["input"]["candidate_release"] = payload["candidate_release"]
    source = FrozenDecisionCase.model_validate(payload)
    ledger.persist_business_mapping_before_framework(source)
    with ledger.serialize_case_execution() as connection:
        event = ledger.commit_event(
            connection,
            case=source,
            framework_run_id=source.framework_run_id,
            result=ExternalResult(
                outcome_code="SYNTHETIC",
                summary="Original synthetic probabilities.",
                key_reasons=("SYNTHETIC",),
                candidate_release=outcome,
            ),
            stage_results=(),
        )
    return event.decision_event_id


def test_complete_probability_population_keeps_low_scores_vetoes_and_missing_labels(
    migrated_settings: Settings,
) -> None:
    from test_standard_outcomes import (
        bind_outcome_input,
        outcome_case,
        standard_observation,
        successor_case,
    )

    registration = registration_case(migrated_settings)
    registration["historical_probability"]["registration"].update(
        start_month="2042-07", end_month="2042-07", capability_version="candidate-v1"
    )
    registered = run_frozen_decision_case(
        migrated_settings, registration, clock=GovernanceClock(registration["knowledge_cutoff"])
    )
    assert registered.report is not None
    from copy import deepcopy

    import pytest

    wrong_registration = deepcopy(registration)
    wrong_registration.update(
        case_id="wrong-source-strategy-registration",
        business_identity="wrong-source-strategy-registration",
    )
    wrong_registration["historical_probability"]["registration"].update(
        version_id="fictional-wrong-strategy-history-v1",
        selection_strategy_version="fictional-unrelated-strategy-v1",
    )
    wrongly_registered = run_frozen_decision_case(
        migrated_settings,
        wrong_registration,
        clock=GovernanceClock(wrong_registration["knowledge_cutoff"]),
    )
    assert wrongly_registered.report is not None
    outcomes = outcome_case(migrated_settings, cutoff="2042-07-02T00:00:00Z")
    source_id = seed_probability_source(migrated_settings)
    wrong_evaluation = evaluation_case(migrated_settings, wrongly_registered.report.event_id)
    wrong_evaluation.update(
        case_id="wrong-strategy-evaluation", business_identity="wrong-strategy-evaluation"
    )
    with pytest.raises(ValueError, match="PROBABILITY_SOURCE_STRATEGY_MISMATCH"):
        run_frozen_decision_case(
            migrated_settings,
            wrong_evaluation,
            clock=GovernanceClock(wrong_evaluation["knowledge_cutoff"]),
        )

    early = run_frozen_decision_case(
        migrated_settings, outcomes, clock=GovernanceClock(outcomes["knowledge_cutoff"])
    )
    assert early.report is not None
    pending_payload = evaluation_case(migrated_settings, registered.report.event_id)
    pending_payload.update(
        case_id="probability-before-late-label", business_identity="probability-before-late-label"
    )
    pending = run_frozen_decision_case(
        migrated_settings,
        pending_payload,
        clock=GovernanceClock(pending_payload["knowledge_cutoff"]),
    )
    assert pending.report is not None and pending.report.result.historical_probability is not None
    assert pending.report.result.historical_probability.counts is not None
    assert pending.report.result.historical_probability.counts.due_missing == 10
    assert pending.report.result.historical_probability.counts.immature == 0
    outcomes = successor_case(outcomes, early.report.event_id, "2043-03-02T00:00:00Z")
    outcomes["standard_outcomes"]["observations"] = [standard_observation()]
    bind_outcome_input(outcomes)
    standard = run_frozen_decision_case(
        migrated_settings, outcomes, clock=GovernanceClock(outcomes["knowledge_cutoff"])
    )
    assert standard.report is not None
    payload = evaluation_case(migrated_settings, registered.report.event_id)
    payload.update(
        knowledge_cutoff="2043-03-03T00:00:00Z", report_generated_at="2043-03-03T00:00:00Z"
    )
    payload["historical_probability"].update(
        cutoff_at=payload["knowledge_cutoff"], previous_event_id=pending.report.event_id
    )

    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None
    evidence = run.report.result.model_dump(mode="json")["historical_probability"]
    assert evidence["counts"]["registered"] == 10
    assert evidence["counts"]["due"] == 10
    assert evidence["counts"]["evaluable"] == 1
    assert evidence["counts"]["due_missing"] == 9
    assert evidence["disposition"] == "INDETERMINATE"
    assert evidence["months"][0]["source_event_id"] == source_id
    assert {m["security_id"] for m in evidence["months"][0]["members"]} == {
        f"synthetic-security-{i:02}" for i in range(10)
    }
    assert evidence["months"][0]["has_candidates"] is False


def test_reports_append_versions_and_scoped_readback_keeps_old_evidence(
    migrated_settings: Settings,
) -> None:
    from copy import deepcopy

    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.modules.delivery.access import AccessPrincipal

    registration = registration_case(migrated_settings)
    saved = run_frozen_decision_case(
        migrated_settings, registration, clock=GovernanceClock(registration["knowledge_cutoff"])
    )
    assert saved.report is not None
    payload = evaluation_case(migrated_settings, saved.report.event_id)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert first.report is not None
    successor = deepcopy(payload)
    successor.update(
        case_id="probability-successor",
        business_identity="probability-successor",
        knowledge_cutoff="2043-04-01T00:00:00Z",
        report_generated_at="2043-04-01T00:00:00Z",
    )
    successor["historical_probability"].update(
        cutoff_at=successor["knowledge_cutoff"], previous_event_id=first.report.event_id
    )
    successor["input"]["historical_probability"] = successor["historical_probability"]
    second = run_frozen_decision_case(
        migrated_settings, successor, clock=GovernanceClock(successor["knowledge_cutoff"])
    )
    assert second.report is not None and second.report.result.historical_probability is not None
    assert (
        second.report.result.historical_probability.previous_report_id
        == first.report.report_version_id
    )
    assert second.report.result.historical_probability.report_version == 2
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    assert delivery.read_report(first.report.report_version_id, principal) == first.report
    assert (
        run_frozen_decision_case(
            migrated_settings, successor, clock=GovernanceClock(successor["knowledge_cutoff"])
        ).report
        == second.report
    )


def test_conflicting_registration_scope_and_cutoff_are_rejected(
    migrated_settings: Settings,
) -> None:
    from copy import deepcopy

    import pytest

    registration = registration_case(migrated_settings)
    saved = run_frozen_decision_case(
        migrated_settings, registration, clock=GovernanceClock(registration["knowledge_cutoff"])
    )
    assert saved.report is not None
    changed = deepcopy(registration)
    changed.update(case_id="probability-conflict", business_identity="probability-conflict")
    changed["historical_probability"]["registration"]["coverage_floor"] = "0.6"
    with pytest.raises(ValueError, match="IDENTITY_ALREADY_BOUND"):
        run_frozen_decision_case(
            migrated_settings, changed, clock=GovernanceClock(changed["knowledge_cutoff"])
        )
    forbidden = evaluation_case(migrated_settings, saved.report.event_id)
    forbidden["access_scope"]["user_id"] = "fictional-other-owner"
    with pytest.raises(ValueError, match="PREREGISTRATION_UNAVAILABLE"):
        run_frozen_decision_case(
            migrated_settings, forbidden, clock=GovernanceClock(forbidden["knowledge_cutoff"])
        )
    late = registration_case(migrated_settings)
    late.update(
        case_id="probability-late-registration",
        business_identity="probability-late-registration",
        knowledge_cutoff="2043-01-01T00:00:00Z",
        report_generated_at="2043-01-01T00:00:00Z",
    )
    late["historical_probability"]["cutoff_at"] = late["knowledge_cutoff"]
    with pytest.raises(ValueError, match="PREREGISTRATION_TOO_LATE"):
        run_frozen_decision_case(
            migrated_settings, late, clock=GovernanceClock(late["knowledge_cutoff"])
        )


def test_complete_selection_abstention_and_failed_attempt_have_distinct_availability(
    migrated_settings: Settings,
) -> None:
    from copy import deepcopy

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase, StageResult

    selection = selection_payload(migrated_settings, security_count=2)
    registration = registration_case(migrated_settings)
    registration["historical_probability"]["registration"].update(
        start_month="2042-05",
        end_month="2042-06",
        selection_strategy_version=selection["selection"]["strategy_version"],
    )
    registered = run_frozen_decision_case(
        migrated_settings, registration, clock=GovernanceClock(registration["knowledge_cutoff"])
    )
    assert registered.report is not None
    selected = run_frozen_decision_case(
        migrated_settings, selection, clock=GovernanceClock(selection["report_generated_at"])
    )
    assert selected.report is not None and selected.report.result.selection is not None
    assert selected.report.result.selection.disposition == "ABSTAINED"
    failed = deepcopy(selection)
    cutoff = "2042-06-30T15:59:59Z"
    failed.update(
        case_id="fictional-failed-selection",
        business_identity="fictional-failed-selection",
        knowledge_cutoff=cutoff,
        report_generated_at=cutoff,
    )
    failed["selection"]["cutoff_at"] = cutoff
    failed["input"]["selection"] = failed["selection"]
    original = FrozenDecisionCase.model_validate(failed)
    ledger = DecisionLedger.from_settings(migrated_settings, clock=GovernanceClock(cutoff))
    ledger.persist_business_mapping_before_framework(original)
    with ledger.serialize_case_execution() as connection:
        ledger.record_stage_result(
            connection,
            case=original,
            stage_result=StageResult(
                phase="FRAMEWORK_RUN",
                status="FAILED",
                gate_results=(),
                reasons=("FICTIONAL_SYSTEM_FAILURE",),
            ),
            recorded_at=cutoff,
        )
    payload = evaluation_case(migrated_settings, registered.report.event_id)
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None and run.report.result.historical_probability is not None
    evidence = run.report.result.historical_probability
    assert evidence.counts is not None
    assert evidence.counts.valid_months == 1
    assert evidence.counts.availability_failures == 1
    assert evidence.months[0].disposition == "SELECTION_ABSTAINED"
    assert evidence.months[1].disposition == "SYSTEM_FAILED"
    assert evidence.inference is not None
    assert evidence.inference.gates["coverage"].estimate == 0
    assert evidence.inference.gates["availability"].estimate == 0.5
