"""Historical evidence through frozen cases, immutable reports and scoped reads."""

from fractions import Fraction
from typing import Any

from test_scoped_qualification import GovernanceClock, case_payload
from test_selection_cohort import selection_payload

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.evaluation.cohort_metrics import decimal_fraction, historical_sessions


def registration_case(settings: Settings) -> dict[str, Any]:
    payload = case_payload(
        settings, "historical-registration", {}, contract_version="historical-selection.1.0.0"
    )
    payload.pop("governance")
    payload["knowledge_cutoff"] = "2033-01-01T00:00:00Z"
    payload["report_generated_at"] = payload["knowledge_cutoff"]
    payload["historical_selection"] = {
        "operation": "REGISTER",
        "synthetic": True,
        "generator_version": "fictional-historical-selection/1",
        "seed": 2020,
        "cutoff_at": payload["knowledge_cutoff"],
        "registration": {
            "version_id": "fictional-historical-contract-v1",
            "start_month": "2033-01",
            "end_month": "2033-12",
            "source_version_bundle": {
                **payload["version_bundle"],
                **{
                    key: "selection.1.0.0"
                    for key in (
                        "case_contract_version",
                        "host_contract_version",
                        "report_projection_contract_version",
                    )
                },
            },
            "strategy_version": "fictional-screening-v1",
            "market_calendar_version": "synthetic-market-calendar-v1",
            "standard_quantity": "100",
            "selection_policy": {
                "version_id": "fictional-selection-v1",
                "cohort_size": 4,
                "industry_limit": 2,
                "capitalization_limit": 2,
                "correlation_sessions": 8,
                "maximum_correlation": "0.8",
                "positive_weight": 3,
                "terminal_weight": 2,
            },
            "positive_members_required": 3,
            "target_members_required": 2,
            "terminal_target": "0.25",
            "maximum_drawdown": "0.15",
            "exploratory_months": 3,
            "formal_months": 6,
            "overall_pass_floor": "0.75",
            "overall_drawdown_floor": "0.75",
            "regime_pass_floor": "0.4",
            "regime_drawdown_floor": "0.5",
            "regime_months": 2,
            "regime_drawdown_months": 2,
            "regime_nonoverlapping_windows": 1,
            "regime_periods": 1,
            "block_lengths": [6, 9, 12],
            "confidence": "0.95",
            "bootstrap_repetitions": 199,
            "random_trials": 37,
            "minimum_availability": "0.95",
        },
        "registration_event_id": None,
        "months": [],
        "previous_event_id": None,
    }
    payload["input"]["historical_selection"] = payload["historical_selection"]
    return payload


def test_preregistration_is_saved_before_results_without_granting_authority(
    migrated_settings: Settings,
) -> None:
    payload = registration_case(migrated_settings)
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None
    evidence = run.report.result.model_dump(mode="json")["historical_selection"]
    assert evidence["disposition"] == "REGISTERED"
    assert evidence["actionable"] is False
    assert evidence["authorization_granted"] is False
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
        ).report
        == run.report
    )


def evaluation_case(
    settings: Settings, registration_event_id: str, *, cutoff: str = "2035-01-01T00:00:00Z"
) -> dict[str, Any]:
    payload = registration_case(settings)
    payload["case_id"] = "fictional-historical-evaluation"
    payload["business_identity"] = payload["case_id"]
    payload["knowledge_cutoff"] = cutoff
    payload["report_generated_at"] = cutoff
    payload["historical_selection"].update(
        operation="EVALUATE",
        cutoff_at=cutoff,
        registration=None,
        registration_event_id=registration_event_id,
    )
    payload["input"]["historical_selection"] = payload["historical_selection"]
    return payload


def test_missing_months_cannot_be_deleted_from_registered_timeline(
    migrated_settings: Settings,
) -> None:
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
    evidence = run.report.result.model_dump(mode="json")["historical_selection"]
    assert evidence["disposition"] == "INDETERMINATE"
    assert evidence["counts"]["planned"] == 12
    assert evidence["counts"]["missing_months"] == 12
    assert [month["plan_month"] for month in evidence["months"]] == [
        f"2033-{month:02}" for month in range(1, 13)
    ]


def registered_selection_case(settings: Settings, *, security_count: int = 12) -> dict[str, Any]:
    source = selection_payload(settings, security_count=security_count)
    registration = registration_case(settings)
    registration["historical_selection"]["registration"].update(
        start_month="2042-05",
        end_month="2042-05",
        strategy_version=source["selection"]["strategy_version"],
        source_version_bundle=source["version_bundle"],
        selection_policy=source["selection"]["policy"],
        positive_members_required=5,
        target_members_required=3,
    )
    registered = run_frozen_decision_case(
        settings, registration, clock=GovernanceClock(registration["knowledge_cutoff"])
    )
    assert registered.report is not None
    selected = run_frozen_decision_case(
        settings, source, clock=GovernanceClock(source["report_generated_at"])
    )
    assert selected.report is not None
    payload = evaluation_case(settings, registered.report.event_id, cutoff="2043-03-01T00:00:00Z")
    payload["historical_selection"]["months"] = [
        {
            "plan_month": "2042-05",
            "selection_event_id": selected.report.event_id,
            "standard_event_id": None,
            "observations": [],
            "paths": [],
            "index": None,
            "factors": None,
            "future_index": None,
        }
    ]
    payload["input"]["historical_selection"] = payload["historical_selection"]
    return payload


def test_selection_abstention_counts_as_mature_failed_batch_without_drawdown_success(
    migrated_settings: Settings,
) -> None:
    payload = registered_selection_case(migrated_settings, security_count=2)
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None
    evidence = run.report.result.model_dump(mode="json")["historical_selection"]
    assert evidence["counts"]["mature_valid"] == 1
    assert evidence["counts"]["passed_batches"] == 0
    assert evidence["counts"]["drawdown_evaluable"] == 0
    assert evidence["months"][0]["disposition"] == "ABSTAINED"
    assert evidence["months"][0]["batch_pass"] is False
    assert evidence["months"][0]["drawdown_pass"] is None


def with_standard_evidence(
    settings: Settings, payload: dict[str, Any], *, stressed: bool = False
) -> dict[str, Any]:
    from copy import deepcopy
    from datetime import datetime, timedelta

    from stock_profiler.modules.portfolio.market_calendar import (
        six_month_terminal_evaluation_at,
        synthetic_market_calendar,
    )

    calendar = synthetic_market_calendar("synthetic-market-calendar-v1")
    assert calendar is not None
    entry_session = calendar.sessions_after_open(
        datetime.fromisoformat("2042-05-30T15:59:59+00:00"), 5
    )[0]
    entry_at = entry_session.closed_at
    terminal_at = six_month_terminal_evaluation_at(entry_at, calendar.version_id)
    zero_costs = {"commission": "0", "fees": "0", "taxes": "0", "slippage": "0"}

    def fact(identity: str, at: datetime) -> dict[str, Any]:
        return {
            "evidence_id": identity,
            "authority": "EXCHANGE",
            "source_version": "fictional-market/1",
            "effective_at": at.isoformat(),
            "published_at": (at + timedelta(seconds=1)).isoformat(),
            "acquired_at": (at + timedelta(seconds=2)).isoformat(),
            "validated_at": (at + timedelta(seconds=3)).isoformat(),
            "corrects_evidence_id": None,
            "correction_reason": None,
        }

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger

    ledger = DecisionLedger.from_settings(settings)
    with ledger.serialize_case_execution() as connection:
        source = ledger.get_decision_event(
            payload["historical_selection"]["months"][0]["selection_event_id"], connection
        )
    assert source is not None and source.result.selection is not None
    selected_members = source.result.selection.members
    observations = []
    paths = []
    sessions = tuple(
        session
        for session in historical_sessions(calendar)
        if entry_at <= session.closed_at <= terminal_at
    )
    for index in range(12):
        security = f"XQZ-SELECT-{index:03}"
        price = (
            ("13", "13", "13", "11", "11", "9")[selected_members.index(security)]
            if security in selected_members
            else "9"
        )
        terminal = {
            **fact(f"fictional-terminal-{security}", terminal_at),
            "price": price,
            "quantity_multiplier": "1",
            "cash_distributions": "0",
            "actions_complete_through": terminal_at.isoformat(),
            "costs": zero_costs,
        }
        observations.append(
            {
                "security_id": security,
                "entry": {
                    **fact(f"fictional-entry-{security}", entry_at),
                    "sessions": [
                        {
                            "closed_at": entry_at.isoformat(),
                            "buyable": True,
                            "unavailable_reason": None,
                            "turnover": "10000",
                            "volume": "1000",
                            "costs": zero_costs,
                        }
                    ],
                },
                "terminal": terminal,
            }
        )
        marks = [
            {
                **fact(f"fictional-mark-{security}-{session.ordinal}", session.closed_at),
                "price": price
                if session.closed_at == terminal_at
                else "5"
                if stressed and offset == 20
                else "10",
                "quantity_multiplier": "1",
                "cash_distributions": "0",
                "actions_complete_through": session.closed_at.isoformat(),
                "costs": zero_costs,
            }
            for offset, session in enumerate(sessions)
        ]
        paths.append({"security_id": security, "marks": marks})
    month = payload["historical_selection"]["months"][0]
    standard = deepcopy(payload)
    standard.pop("historical_selection")
    standard["input"].pop("historical_selection")
    for key in (
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    ):
        standard["version_bundle"][key] = "standard-outcomes.1.0.0"
    standard["case_id"] = "fictional-standard-history"
    standard["business_identity"] = standard["case_id"]
    standard["standard_outcomes"] = {
        "contract_version": "1.0.0",
        "synthetic": True,
        "generator_version": "fictional-historical-selection/1",
        "seed": 2020,
        "selection_event_id": month["selection_event_id"],
        "cutoff_at": payload["knowledge_cutoff"],
        "market_calendar_version": calendar.version_id,
        "standard_quantity": "100",
        "observations": [row for row in observations if row["security_id"] in selected_members],
        "previous_event_id": None,
    }
    from stock_profiler.modules.evaluation.contracts import StandardOutcomeCommand

    standard["standard_outcomes"] = StandardOutcomeCommand.model_validate(
        standard["standard_outcomes"]
    ).model_dump(mode="json")
    standard["input"]["standard_outcomes"] = standard["standard_outcomes"]
    recorded = run_frozen_decision_case(
        settings, standard, clock=GovernanceClock(standard["knowledge_cutoff"])
    )
    assert recorded.report is not None
    month.update(standard_event_id=recorded.report.event_id, observations=observations, paths=paths)
    from stock_profiler.modules.evaluation.historical_contracts import HistoricalSelectionCommand

    payload["historical_selection"] = HistoricalSelectionCommand.model_validate(
        payload["historical_selection"]
    ).model_dump(mode="json")
    payload["input"]["historical_selection"] = payload["historical_selection"]
    return payload


def test_standard_batch_success_and_equal_sleeve_drawdown_are_independent(
    migrated_settings: Settings,
) -> None:
    payload = with_standard_evidence(
        migrated_settings, registered_selection_case(migrated_settings), stressed=True
    )
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None
    month = run.report.result.model_dump(mode="json")["historical_selection"]["months"][0]
    assert month["batch_pass"] is True
    assert month["positive_count"] == 5
    assert month["target_count"] == 3
    assert month["maximum_drawdown"] == "0.5"
    assert month["drawdown_pass"] is False


def with_selection_context(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime, timedelta

    from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar

    calendar = synthetic_market_calendar("synthetic-market-calendar-v1")
    assert calendar is not None
    cutoff = datetime.fromisoformat("2042-05-30T15:59:59+00:00")
    history = tuple(
        session for session in historical_sessions(calendar) if session.closed_at <= cutoff
    )
    fact = {
        "evidence_id": "fictional-index",
        "authority": "EXCHANGE",
        "source_version": "fictional-all-share-total-return/1",
        "effective_at": history[-1].closed_at.isoformat(),
        "published_at": (cutoff - timedelta(seconds=3)).isoformat(),
        "acquired_at": (cutoff - timedelta(seconds=2)).isoformat(),
        "validated_at": (cutoff - timedelta(seconds=1)).isoformat(),
        "corrects_evidence_id": None,
        "correction_reason": None,
    }
    month = payload["historical_selection"]["months"][0]
    month["index"] = {
        **fact,
        "prices": [
            {"closed_at": session.closed_at.isoformat(), "total_return_price": str(1000 + offset)}
            for offset, session in enumerate(history[-120:])
        ],
    }
    month["factors"] = {
        **fact,
        "evidence_id": "fictional-factors",
        "rows": [
            {
                "security_id": f"XQZ-SELECT-{index:03}",
                "net_income_ttm": str(index + 1),
                "total_capitalization": "100",
                "average_equity": "100",
                "momentum_start_price": "10",
                "momentum_end_price": "11",
                "momentum_start_at": history[-252].closed_at.isoformat(),
                "momentum_end_at": history[-21].closed_at.isoformat(),
                "daily_returns": ["0.01" if offset % 2 else "-0.01" for offset in range(120)],
                "return_dates": [session.closed_at.isoformat() for session in history[-120:]],
            }
            for index in range(12)
        ],
    }
    from stock_profiler.modules.evaluation.historical_contracts import HistoricalSelectionCommand

    payload["historical_selection"] = HistoricalSelectionCommand.model_validate(
        payload["historical_selection"]
    ).model_dump(mode="json")
    payload["input"]["historical_selection"] = payload["historical_selection"]
    return payload


def test_paired_baselines_and_market_state_are_host_computed_at_selection_cutoff(
    migrated_settings: Settings,
) -> None:
    payload = with_selection_context(
        migrated_settings,
        with_standard_evidence(migrated_settings, registered_selection_case(migrated_settings)),
    )
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None
    month = run.report.result.model_dump(mode="json")["historical_selection"]["months"][0]
    assert month["regime"] == "BULL"
    assert set(month["baselines"]) == {"UNIVERSE", "RANDOM", "FOUR_FACTOR"}
    assert month["baselines"]["UNIVERSE"]["positive_rate"] == str(decimal_fraction(Fraction(5, 12)))
    assert month["baselines"]["UNIVERSE"]["batch_pass_rate"] is None
    assert month["baselines"]["UNIVERSE"]["counts"] == {
        "positive": 5,
        "target": 3,
        "member_slots": 12,
        "passed_trials": None,
    }
    assert month["baselines"]["RANDOM"]["trial_count"] == 37
    random = month["baselines"]["RANDOM"]
    assert random["counts"]["member_slots"] == 37 * 6
    assert (
        str(decimal_fraction(Fraction(random["counts"]["passed_trials"], random["trial_count"])))
        == random["batch_pass_rate"]
    )
    assert month["baselines"]["FOUR_FACTOR"]["members"] != month["members"]


def test_later_reports_cannot_rewrite_baseline_observations_without_authoritative_correction(
    migrated_settings: Settings,
) -> None:
    from copy import deepcopy

    import pytest

    from stock_profiler.modules.evaluation.historical_contracts import HistoricalSelectionCommand

    payload = with_selection_context(
        migrated_settings,
        with_standard_evidence(migrated_settings, registered_selection_case(migrated_settings)),
    )
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert first.report is not None
    successor = deepcopy(payload)
    successor["case_id"] += ":rewrite"
    successor["business_identity"] += ":rewrite"
    successor["knowledge_cutoff"] = "2043-03-02T00:00:00Z"
    successor["report_generated_at"] = successor["knowledge_cutoff"]
    command = successor["historical_selection"]
    command["cutoff_at"] = successor["knowledge_cutoff"]
    command["previous_event_id"] = first.report.event_id
    assert first.report.result.historical_selection is not None
    observation = next(
        row
        for row in command["months"][0]["observations"]
        if row["security_id"] not in first.report.result.historical_selection.months[0].members
    )
    observation["terminal"]["price"] = "100"
    successor["historical_selection"] = HistoricalSelectionCommand.model_validate(
        command
    ).model_dump(mode="json")
    successor["input"]["historical_selection"] = successor["historical_selection"]
    with pytest.raises(ValueError, match="HISTORICAL_EVIDENCE_IDENTITY_REDEFINED"):
        run_frozen_decision_case(
            migrated_settings, successor, clock=GovernanceClock(successor["knowledge_cutoff"])
        )


def test_uncommitted_system_failure_stays_in_monthly_availability_denominator(
    migrated_settings: Settings,
) -> None:
    from copy import deepcopy

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase, StageResult

    source = selection_payload(migrated_settings)
    registration = registration_case(migrated_settings)
    registration["historical_selection"]["registration"].update(
        start_month="2042-05",
        end_month="2042-05",
        strategy_version=source["selection"]["strategy_version"],
        source_version_bundle=source["version_bundle"],
        selection_policy=source["selection"]["policy"],
        positive_members_required=5,
        target_members_required=3,
    )
    registered = run_frozen_decision_case(
        migrated_settings, registration, clock=GovernanceClock(registration["knowledge_cutoff"])
    )
    assert registered.report is not None
    original = FrozenDecisionCase.model_validate(source)
    ledger = DecisionLedger.from_settings(
        migrated_settings, clock=GovernanceClock(source["report_generated_at"])
    )
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
            recorded_at=source["report_generated_at"],
        )
    payload = evaluation_case(
        migrated_settings, registered.report.event_id, cutoff="2043-03-01T00:00:00Z"
    )
    run = run_frozen_decision_case(
        migrated_settings, deepcopy(payload), clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None
    result = run.report.result.model_dump(mode="json")["historical_selection"]
    assert result["counts"]["planned"] == 1
    assert result["counts"]["availability_failures"] == 1
    assert result["counts"]["missing_months"] == 0
    assert result["counts"]["mature_valid"] == 0
    assert result["months"][0]["disposition"] == "SYSTEM_FAILED"


def test_mature_abstention_retains_selection_visible_market_state(
    migrated_settings: Settings,
) -> None:
    payload = with_selection_context(
        migrated_settings, registered_selection_case(migrated_settings, security_count=2)
    )
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None and run.report.result.historical_selection is not None
    month = run.report.result.historical_selection.months[0]
    assert month.regime == "BULL"
    assert month.batch_pass is False
    assert month.drawdown_pass is None


def test_future_planned_months_are_distinct_from_missing_due_months(
    migrated_settings: Settings,
) -> None:
    registration = registration_case(migrated_settings)
    registered = run_frozen_decision_case(
        migrated_settings, registration, clock=GovernanceClock(registration["knowledge_cutoff"])
    )
    assert registered.report is not None
    payload = evaluation_case(
        migrated_settings, registered.report.event_id, cutoff="2033-01-02T00:00:00Z"
    )
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None and run.report.result.historical_selection is not None
    evidence = run.report.result.historical_selection
    assert evidence.counts is not None and evidence.counts.missing_months == 0
    assert {month.disposition for month in evidence.months} == {"FUTURE"}
    assert evidence.disposition == "INSUFFICIENT"


def test_linked_versions_retain_months_and_keep_previous_reports_scoped(
    migrated_settings: Settings,
) -> None:
    from copy import deepcopy

    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.modules.delivery.access import AccessPrincipal
    from stock_profiler.modules.evaluation.historical_contracts import HistoricalSelectionCommand

    payload = registered_selection_case(migrated_settings, security_count=2)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert first.report is not None
    successor = deepcopy(payload)
    successor["case_id"] += ":next"
    successor["business_identity"] += ":next"
    successor["knowledge_cutoff"] = "2043-03-02T00:00:00Z"
    successor["report_generated_at"] = successor["knowledge_cutoff"]
    successor["historical_selection"].update(
        months=[], previous_event_id=first.report.event_id, cutoff_at=successor["knowledge_cutoff"]
    )
    successor["historical_selection"] = HistoricalSelectionCommand.model_validate(
        successor["historical_selection"]
    ).model_dump(mode="json")
    successor["input"]["historical_selection"] = successor["historical_selection"]
    second = run_frozen_decision_case(
        migrated_settings, successor, clock=GovernanceClock(successor["knowledge_cutoff"])
    )
    assert second.report is not None and second.report.result.historical_selection is not None
    evidence = second.report.result.historical_selection
    assert evidence.previous_report_id == first.report.report_version_id
    assert evidence.report_version == 2
    assert len(evidence.months) == 1
    assert evidence.months[0].disposition == "ABSTAINED"
    scope = payload["access_scope"]
    principal = AccessPrincipal(
        user_id=scope["user_id"],
        account_ids=tuple(scope["account_ids"]),
        permissions=("REPORT_READ",),
    )
    delivery = ResultDelivery.from_settings(migrated_settings)
    assert delivery.read_report(first.report.report_version_id, principal) == first.report
    assert delivery.read_report(second.report.report_version_id, principal) == second.report
    assert delivery.read_report(second.report.report_version_id, None) is None
    assert (
        delivery.read_report(
            second.report.report_version_id,
            principal.model_copy(update={"account_ids": ("fictional-other-account",)}),
        )
        is None
    )


def test_single_missing_volatility_factor_scores_zero_without_losing_month(
    migrated_settings: Settings,
) -> None:
    payload = with_selection_context(
        migrated_settings,
        with_standard_evidence(migrated_settings, registered_selection_case(migrated_settings)),
    )
    month = payload["historical_selection"]["months"][0]
    month["factors"]["rows"][0].update(daily_returns=[], return_dates=[])
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None and run.report.result.historical_selection is not None
    assert set(run.report.result.historical_selection.months[0].baselines) == {
        "UNIVERSE",
        "RANDOM",
        "FOUR_FACTOR",
    }


def test_whole_month_factor_source_failure_is_explicit_availability_failure(
    migrated_settings: Settings,
) -> None:
    payload = with_selection_context(
        migrated_settings,
        with_standard_evidence(migrated_settings, registered_selection_case(migrated_settings)),
    )
    payload["historical_selection"]["months"][0]["factors"] = None
    run = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert run.report is not None and run.report.result.historical_selection is not None
    evidence = run.report.result.historical_selection
    assert evidence.months[0].batch_pass is True
    assert evidence.counts is not None and evidence.counts.availability_failures == 1
    assert evidence.inference is not None
    assert evidence.inference.gates["availability"].estimate == 0


def test_empty_index_with_retrospective_evidence_has_controlled_rejection(
    migrated_settings: Settings,
) -> None:
    import pytest

    from stock_profiler.modules.evaluation.historical_contracts import HistoricalSelectionCommand

    payload = with_selection_context(
        migrated_settings,
        with_standard_evidence(migrated_settings, registered_selection_case(migrated_settings)),
    )
    month = payload["historical_selection"]["months"][0]
    index = month["index"]
    month["future_index"] = {
        **{key: value for key, value in index.items() if key != "prices"},
        "evidence_id": "fictional-retrospective-index",
        "starting_at": index["prices"][-1]["closed_at"],
        "starting_total_return_price": index["prices"][-1]["total_return_price"],
        "terminal_total_return_price": "1200",
    }
    index["prices"] = []
    payload["historical_selection"] = HistoricalSelectionCommand.model_validate(
        payload["historical_selection"]
    ).model_dump(mode="json")
    payload["input"]["historical_selection"] = payload["historical_selection"]
    with pytest.raises(ValueError, match="HISTORICAL_EX_POST_INDEX_EVIDENCE_INVALID"):
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
        )

    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery

    audits = ResultDelivery.from_settings(migrated_settings).audit_history()
    assert len(audits) == 1
    assert audits[0].surface == "HOST"
    assert audits[0].outcome == "DENIED"
