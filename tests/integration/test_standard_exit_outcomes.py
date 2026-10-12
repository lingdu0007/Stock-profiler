"""Independent exit outcomes at the frozen decision-case/report boundary."""

from typing import Any

import pytest
from test_scoped_qualification import GovernanceClock
from test_standard_outcomes import (
    bind_outcome_input,
    outcome_case,
    seed_candidate_snapshot,
    standard_observation,
)

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings


def standard_entry_case(settings: Settings) -> dict[str, Any]:
    payload = outcome_case(settings)
    seed_candidate_snapshot(settings)
    payload["standard_outcomes"]["observations"] = [standard_observation()]
    bind_outcome_input(payload)
    return payload


def test_every_executable_candidate_has_independent_standard_exit_identity(
    migrated_settings: Settings,
) -> None:
    payload = standard_entry_case(migrated_settings)
    result = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert result.report is not None
    outcome = result.report.model_dump(mode="json")["result"]["standard_outcomes"]
    positions = outcome["exit_positions"]
    assert len(positions) == 1
    assert positions[0]["origin"] == "SYSTEM"
    assert positions[0]["initial_exposure"] == "1"
    assert positions[0]["current_exposure"] == "1"
    assert positions[0]["entry_at"] == "2042-07-02T15:00:00Z"
    assert positions[0]["matures_at"] == "2043-01-02T15:00:00Z"
    candidate = next(member for member in outcome["members"] if member["population"] == "CANDIDATE")
    assert positions[0]["candidate_evaluation_id"] == candidate["evaluation_id"]
    assert positions[0]["lifecycle_id"] != candidate["evaluation_id"]
    assert positions[0]["state"] == "UNAVAILABLE"


def exit_case(settings: Settings, command: dict[str, Any]) -> dict[str, Any]:
    from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case

    payload = load_frozen_decision_case(settings).model_dump(mode="json")
    for field in (
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    ):
        payload["version_bundle"][field] = "exit-evaluation.1.0.0"
    payload["agent_definition"]["version"] = "2.0.0"
    payload["version_bundle"]["agent_definition_version"] = "2.0.0"
    payload["knowledge_cutoff"] = command["cutoff_at"]
    payload["report_generated_at"] = command["cutoff_at"]
    payload["case_id"] = "fictional-exit:" + command["operation"] + command["cutoff_at"]
    payload["business_identity"] = payload["case_id"]
    payload["access_scope"] = {
        "contract_version": "1.0.0",
        "user_id": "stock-profiler-single-user",
        "account_ids": ["synthetic-account-4017"],
        "visibility": "USER",
    }
    from stock_profiler.modules.evaluation.exit_contracts import ExitEvaluationCommand

    command = ExitEvaluationCommand.model_validate(command).model_dump(mode="json")
    payload["exit_evaluation"] = command
    payload["input"]["exit_evaluation"] = command
    return payload


def exit_policy() -> dict[str, Any]:
    return {
        "version_id": "fictional-exit-policy/1",
        "start_month": "2042-07",
        "end_month": "2043-01",
        "market_calendar_version": "synthetic-market-calendar-v1",
        "model_version": "fictional-exit-model/1",
        "action_version": "fictional-exit-action/1",
        "cost_version": "fictional-exit-cost/1",
        "cost_curve": {
            "commission_rate": "0",
            "minimum_commission": "5",
            "fee_rate": "0",
            "tax_rate": "0",
            "slippage_rate": "0",
        },
        "baseline_version": "fictional-exit-baseline/1",
        "loss_grids": ["0.05", "0.10", "0.15", "0.20"],
        "selected_grid": "0.10",
        "states": ["BULL", "BEAR", "SIDEWAYS"],
        "bootstrap_repetitions": 99,
        "confidence": "0.95",
        "block_lengths": [6, 9, 12],
        "fixed_reduce_loss": "0.10",
        "fixed_exit_loss": "0.20",
        "trailing_reduce_loss": "0.10",
        "trailing_exit_loss": "0.20",
        "safe_downside": "0.20",
        "risky_downside": "0.40",
        "established_value": "0.70",
        "denied_value": "0.30",
    }


def register_exit_policy(settings: Settings) -> Any:
    command: dict[str, Any] = {
        "operation": "REGISTER",
        "synthetic": True,
        "generator_version": "fictional-exit/1",
        "seed": 3131,
        "cutoff_at": "2042-06-01T16:00:00Z",
        "policy": exit_policy(),
        "registration_event_id": None,
        "source_standard_event_id": None,
        "previous_event_id": None,
        "observations": [],
    }
    payload = exit_case(settings, command)
    return run_frozen_decision_case(settings, payload, clock=GovernanceClock(command["cutoff_at"]))


def test_exit_policy_is_saved_before_observed_outcomes_and_grants_no_authority(
    migrated_settings: Settings,
) -> None:
    result = register_exit_policy(migrated_settings)
    assert result.report is not None
    outcome = result.report.model_dump(mode="json")["result"]["exit_evaluation"]
    assert outcome["disposition"] == "REGISTERED"
    assert outcome["policy"]["block_lengths"] == [6, 9, 12]
    assert outcome["authorization_granted"] is False
    assert outcome["actionable"] is False


def exit_observation(lifecycle_id: str, *, end: str = "2042-07-08T15:00:00Z") -> dict[str, Any]:
    from datetime import datetime

    from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar

    calendar = synthetic_market_calendar("synthetic-market-calendar-v1")
    assert calendar is not None
    start = datetime.fromisoformat("2042-07-02T15:00:00+00:00")
    sessions = calendar.recent_completed_sessions(datetime.fromisoformat(end), 1000)
    dates = [session.closed_at for session in sessions if session.closed_at >= start]
    costs = {
        "commission_rate": "0",
        "minimum_commission": "5",
        "fee_rate": "0",
        "tax_rate": "0",
        "slippage_rate": "0",
    }
    return {
        "lifecycle_id": lifecycle_id,
        "evidence_id": "fictional-exit-path/1",
        "authority": "EXCHANGE",
        "source_version": "fictional-exit-path/1",
        "effective_at": end,
        "published_at": end,
        "acquired_at": end,
        "validated_at": end,
        "corrects_evidence_id": None,
        "correction_reason": None,
        "sessions": [
            {
                "closed_at": day.isoformat(),
                "price": "10",
                "vwap": "10",
                "sellable": True,
                "blocked_reason": None,
                "quantity_multiplier": "1",
                "cash_per_initial_share": "0",
                "costs": costs,
            }
            for day in dates
        ],
        "forecasts": [
            {
                "prediction_id": f"fictional-prediction-{i}",
                "cutoff_at": day.isoformat(),
                "frozen_at": day.isoformat(),
                "training_watermark_at": "2042-06-01T15:00:00Z",
                "model_version": "fictional-exit-model/1",
                "state": "BULL",
                "data_complete": True,
                "qualified_scope": True,
                "new_evidence_id": "fictional-common-basis/1",
                "downside": {
                    grid: {
                        "point": "0.5" if i < 2 else "0.1",
                        "lower_error": "0",
                        "upper_error": "0",
                    }
                    for grid in ("0.05", "0.10", "0.15", "0.20")
                },
                "value": {"point": "0.8", "lower_error": "0", "upper_error": "0"},
                "hard_exit": False,
                "hard_evidence_id": None,
            }
            for i, day in enumerate(dates)
        ],
    }


def observe_exit(
    settings: Settings,
    *,
    mutate: Any = None,
    cutoff: str = "2042-07-08T16:00:00Z",
    freeze: bool = True,
) -> Any:
    from copy import deepcopy

    registration = register_exit_policy(settings)
    assert registration.report is not None
    payload = standard_entry_case(settings)
    payload["knowledge_cutoff"] = "2042-07-02T16:00:00Z"
    payload["report_generated_at"] = payload["knowledge_cutoff"]
    payload["standard_outcomes"]["cutoff_at"] = payload["knowledge_cutoff"]
    payload["standard_outcomes"]["observations"][0]["terminal"] = None
    bind_outcome_input(payload)
    standard = run_frozen_decision_case(
        settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert standard.report is not None
    lifecycle = standard.report.model_dump(mode="json")["result"]["standard_outcomes"][
        "exit_positions"
    ][0]["lifecycle_id"]
    observation = exit_observation(lifecycle)
    if mutate:
        mutate(observation)
    if freeze:
        freeze_predictions(settings, registration.report, standard.report.event_id, observation)
    command = {
        "operation": "OBSERVE",
        "synthetic": True,
        "generator_version": "fictional-exit/1",
        "seed": 3131,
        "cutoff_at": cutoff,
        "policy": None,
        "registration_event_id": registration.report.event_id,
        "source_standard_event_id": standard.report.event_id,
        "previous_event_id": None,
        "observations": [deepcopy(observation)],
    }
    request = exit_case(settings, command)
    return run_frozen_decision_case(settings, request, clock=GovernanceClock(cutoff))


def test_repeated_reductions_and_hold_never_restore_sold_standard_exposure(
    migrated_settings: Settings,
) -> None:
    execution = observe_exit(migrated_settings)
    assert execution.report is not None
    position = execution.report.model_dump(mode="json")["result"]["exit_evaluation"]["positions"][0]
    assert position["current_exposure"] == "0.5"
    assert [trade["exposure_sold"] for trade in position["policies"]["PRIMARY"]["trades"]] == [
        "0.5"
    ]
    assert position["policies"]["PRIMARY"]["trades"][0]["executed_at"] == "2042-07-03T15:00:00Z"
    assert position["state"] == "PENDING"
    assert position["candidate_evaluation_id"] != position["lifecycle_id"]


def test_blocked_exit_keeps_market_exposure_until_first_actual_sell_window(
    migrated_settings: Settings,
) -> None:
    def block_path(observation: dict[str, Any]) -> None:
        for row in observation["sessions"][1:3]:
            row.update(sellable=False, blocked_reason="NO_BIDS", price="8", vwap="0")
        observation["sessions"][3].update(price="8", vwap="8")

    result = observe_exit(migrated_settings, mutate=block_path)
    assert result.report is not None
    policy = result.report.model_dump(mode="json")["result"]["exit_evaluation"]["positions"][0][
        "policies"
    ]["PRIMARY"]
    assert len(policy["trades"]) == 1
    assert policy["trades"][0]["executed_at"] == "2042-07-07T15:00:00Z"
    assert policy["trades"][0]["price"] == "8"
    assert policy["trades"][0]["net_proceeds"] == "395.0"
    assert float(policy["daily_wealth"][1]) == 800


def test_continuous_forward_targets_and_six_month_wealth_use_separate_horizons(
    migrated_settings: Settings,
) -> None:
    import pytest

    def mature_path(observation: dict[str, Any]) -> None:
        full = exit_observation(observation["lifecycle_id"], end="2043-03-31T15:00:00Z")
        full["forecasts"] = [
            forecast
            for forecast in full["forecasts"]
            if forecast["cutoff_at"] <= "2043-01-02T15:00:00+00:00"
        ]
        for i, row in enumerate(full["sessions"]):
            row["price"] = row["vwap"] = (
                "10" if i == 0 else "11" if i == 1 else "8" if i == 2 else "12"
            )
        observation.update(full)

    result = observe_exit(migrated_settings, mutate=mature_path, cutoff="2043-04-01T16:00:00Z")
    assert result.report is not None
    position = result.report.model_dump(mode="json")["result"]["exit_evaluation"]["positions"][0]
    first = position["decisions"][0]
    assert float(first["downside_return"]) == pytest.approx(-0.20100502512562814)
    assert float(first["value_return"]) == pytest.approx(0.20100502512562814)
    assert float(first["upside_capture"]) == pytest.approx(0.725)
    assert float(position["policies"]["PRIMARY"]["net_total_return"]) == pytest.approx(
        0.12871287128712872
    )
    assert position["state"] == "EVALUABLE"


def test_historical_evaluation_keeps_whole_lifecycle_denominators_and_due_gaps(
    migrated_settings: Settings,
) -> None:
    observed = observe_exit(migrated_settings)
    assert observed.report is not None
    observation = observed.report.result.exit_evaluation
    assert observation is not None
    command: dict[str, Any] = {
        "operation": "EVALUATE",
        "synthetic": True,
        "generator_version": "fictional-exit/1",
        "seed": 3131,
        "cutoff_at": "2043-04-01T16:00:00Z",
        "policy": None,
        "registration_event_id": observation.registration_event_id,
        "source_standard_event_id": None,
        "previous_event_id": None,
        "observations": [],
    }
    result = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert result.report is not None
    report = result.report.model_dump(mode="json")["result"]["exit_evaluation"]
    assert report["counts"] == {
        "registered": 1,
        "due": 1,
        "evaluable": 0,
        "due_missing": 1,
        "immature": 0,
    }
    assert report["inference"]["disposition"] == "INDETERMINATE"
    assert report["inference"]["watermarks"]["overall"]["position_count"] == 0
    assert set(report["inference"]["resampling"]) == {"6", "9", "12"}
    assert all(item["calendar_months"] == 7 for item in report["inference"]["resampling"].values())
    assert report["authorization_granted"] is False
    gates = report["inference"]["gates"]
    assert all(
        f"overall:net:{name}" in gates
        for name in ("HOLD_END", "HARD_ONLY", "FIXED_LOSS", "TRAILING_DRAWDOWN")
    )
    assert {
        "overall:upside_capture",
        "overall:cost",
        "overall:cost_to_gross",
        "overall:stability",
        "overall:coverage",
        "overall:replay",
    }.issubset(gates)


def test_extending_outcome_evidence_cannot_rewrite_original_frozen_predictions(
    migrated_settings: Settings,
) -> None:
    import pytest

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger

    original = observe_exit(migrated_settings)
    assert original.report is not None
    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_decision_event(original.report.event_id, connection)
    assert fact is not None and fact.case.exit_evaluation is not None
    command = fact.case.exit_evaluation.model_dump(mode="json")
    command["cutoff_at"] = "2042-07-09T16:00:00Z"
    command["previous_event_id"] = original.report.event_id
    observation = command["observations"][0]
    observation["evidence_id"] = "fictional-corrected-exit-path/2"
    observation["corrects_evidence_id"] = "fictional-exit-path/1"
    observation["correction_reason"] = "Fictional market evidence correction."
    for clock in ("published_at", "acquired_at", "validated_at"):
        observation[clock] = "2042-07-09T15:00:00Z"
    observation["forecasts"][0]["value"]["point"] = "0.1"
    request = exit_case(migrated_settings, command)
    with pytest.raises(ValueError, match="EXIT_FROZEN_FORECAST_CHANGED"):
        run_frozen_decision_case(
            migrated_settings, request, clock=GovernanceClock(command["cutoff_at"])
        )


def test_many_daily_predictions_remain_one_independent_lifecycle(
    migrated_settings: Settings,
) -> None:
    def mature_path(observation: dict[str, Any]) -> None:
        full = exit_observation(observation["lifecycle_id"], end="2043-03-31T15:00:00Z")
        full["forecasts"] = [
            forecast
            for forecast in full["forecasts"]
            if forecast["cutoff_at"] <= "2043-01-02T15:00:00+00:00"
        ]
        for forecast in full["forecasts"]:
            forecast["baseline_probabilities"] = {
                name: {
                    target: "0.5"
                    for target in ("V60", "D20:0.05", "D20:0.10", "D20:0.15", "D20:0.20")
                }
                for name in ("STATE_RATE", "TRANSPARENT")
            }
            forecast["quantiles"] = {
                target: "0" for target in ("V60", "D20:0.05", "D20:0.10", "D20:0.15", "D20:0.20")
            }
        observation.update(full)

    observed = observe_exit(migrated_settings, mutate=mature_path, cutoff="2043-04-01T16:00:00Z")
    assert observed.report is not None and observed.report.result.exit_evaluation is not None
    command: dict[str, Any] = {
        "operation": "EVALUATE",
        "synthetic": True,
        "generator_version": "fictional-exit/1",
        "seed": 3131,
        "cutoff_at": "2043-04-02T16:00:00Z",
        "policy": None,
        "registration_event_id": observed.report.result.exit_evaluation.registration_event_id,
        "source_standard_event_id": None,
        "previous_event_id": None,
        "observations": [],
    }
    result = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert result.report is not None and result.report.result.exit_evaluation is not None
    evidence = result.report.result.exit_evaluation
    assert evidence.inference is not None and evidence.counts is not None
    assert evidence.inference.disposition == "INSUFFICIENT"
    assert evidence.counts.evaluable == 1
    mark = evidence.inference.watermarks["overall"]
    assert mark.position_count == 1
    assert mark.mature_months == 1
    assert mark.directions == {"HOLD": 1, "REDUCE": 1, "EXIT": 0}
    assert all(count == 1 for count in mark.target_positions.values())
    assert evidence.inference.diagnostics["overall:D20:0.10"].brier_score > 0
    assert evidence.inference.pinball_loss["overall:D20:0.10"] is not None
    assert evidence.inference.gates["overall:replay"].estimate == 1
    assert all(
        gate.applicable is False
        for name, gate in evidence.inference.gates.items()
        if name.startswith("BEAR:")
    )


def evaluate_observed(
    settings: Settings, observed: Any, *, cutoff: str = "2043-04-02T16:00:00Z"
) -> Any:
    assert observed.report is not None and observed.report.result.exit_evaluation is not None
    command = {
        "operation": "EVALUATE",
        "synthetic": True,
        "generator_version": "fictional-exit/1",
        "seed": 3131,
        "cutoff_at": cutoff,
        "policy": None,
        "registration_event_id": observed.report.result.exit_evaluation.registration_event_id,
        "source_standard_event_id": None,
        "previous_event_id": None,
        "observations": [],
    }
    result = run_frozen_decision_case(
        settings, exit_case(settings, command), clock=GovernanceClock(command["cutoff_at"])
    )
    assert result.report is not None and result.report.result.exit_evaluation is not None
    return result.report.result.exit_evaluation


def test_due_forward_labels_are_missing_evidence_even_with_complete_six_month_wealth(
    migrated_settings: Settings,
) -> None:
    def mature_only(observation: dict[str, Any]) -> None:
        observation.update(
            exit_observation(observation["lifecycle_id"], end="2043-01-02T15:00:00Z")
        )

    observed = observe_exit(migrated_settings, mutate=mature_only, cutoff="2043-04-01T16:00:00Z")
    evidence = evaluate_observed(migrated_settings, observed)
    assert evidence.counts.evaluable == 1
    assert evidence.inference.disposition == "INDETERMINATE"
    assert any(
        reason.startswith("FORWARD_LABEL_MISSING:")
        for reason in evidence.inference.missing_evidence
    )


def test_complete_forecasts_without_paired_calibration_baselines_cannot_hide_gate(
    migrated_settings: Settings,
) -> None:
    def full_path(observation: dict[str, Any]) -> None:
        full = exit_observation(observation["lifecycle_id"], end="2043-03-31T15:00:00Z")
        full["forecasts"] = [
            item for item in full["forecasts"] if item["cutoff_at"] <= "2043-01-02T15:00:00+00:00"
        ]
        observation.update(full)

    observed = observe_exit(migrated_settings, mutate=full_path, cutoff="2043-04-01T16:00:00Z")
    evidence = evaluate_observed(migrated_settings, observed)
    gate = evidence.inference.gates["overall:increment:V60:STATE_RATE:brier"]
    assert gate.applicable is True and gate.passed is None
    assert evidence.inference.disposition == "INDETERMINATE"


def test_hard_exit_identity_alone_is_not_authoritative_evidence(
    migrated_settings: Settings,
) -> None:
    import pytest

    def unverified(observation: dict[str, Any]) -> None:
        observation["forecasts"][0].update(
            hard_exit=True, hard_evidence_id="fictional-unverified-hard-fact"
        )

    with pytest.raises(ValueError, match="EXIT_HARD_EVIDENCE_UNAVAILABLE"):
        observe_exit(migrated_settings, mutate=unverified)


def test_statistical_exit_is_terminal_for_standard_exposure_and_replays_saved_report(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger

    def exit_then_hold(observation: dict[str, Any]) -> None:
        observation["forecasts"][0]["value"]["point"] = "0.1"

    result = observe_exit(migrated_settings, mutate=exit_then_hold)
    assert result.report is not None
    position = result.report.result.exit_evaluation.positions[0]
    assert position.current_exposure == 0
    assert position.policies["PRIMARY"].turnover == 1
    assert len(position.policies["PRIMARY"].trades) == 1
    assert position.policies["HOLD_END"].current_exposure == 1
    assert position.policies["HARD_ONLY"].current_exposure == 1
    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        saved = ledger.get_decision_event(result.report.event_id, connection)
    assert saved is not None
    replay = run_frozen_decision_case(
        migrated_settings,
        saved.case.model_dump(mode="json"),
        clock=GovernanceClock("2042-07-08T16:00:00Z"),
    )
    assert replay.report == result.report
    import json
    import sys

    from stock_profiler.entrypoints import cli

    monkeypatch.setattr(cli, "load_settings", lambda: migrated_settings)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stock-profiler",
            "decision-case-replay",
            "--business-identity",
            saved.case.business_identity,
        ],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["report"] == result.report.model_dump(mode="json")


def test_authoritative_hard_exit_is_shared_by_every_risk_baseline(
    migrated_settings: Settings,
) -> None:
    def hard_fact(observation: dict[str, Any]) -> None:
        forecast = observation["forecasts"][0]
        fact = {
            field: observation[field]
            for field in (
                "authority",
                "source_version",
                "corrects_evidence_id",
                "correction_reason",
            )
        }
        fact.update(evidence_id="fictional-exchange-hard-fact", reason="AUTHORITATIVE_DELISTING")
        for field in ("effective_at", "published_at", "acquired_at", "validated_at"):
            fact[field] = forecast["cutoff_at"]
        forecast.update(hard_exit=True, hard_evidence_id=fact["evidence_id"], hard_evidence=fact)

    result = observe_exit(migrated_settings, mutate=hard_fact)
    assert result.report is not None
    position = result.report.result.exit_evaluation.positions[0]
    assert position.decisions[0].statistical is False
    for name in ("PRIMARY", "HARD_ONLY", "FIXED_LOSS", "TRAILING_DRAWDOWN"):
        assert position.policies[name].current_exposure == 0
        assert position.policies[name].trades == position.policies["PRIMARY"].trades
    assert position.policies["HOLD_END"].current_exposure == 1


def test_outcome_upload_cannot_introduce_predictions_that_were_never_committed(
    migrated_settings: Settings,
) -> None:
    import pytest

    with pytest.raises(ValueError, match="EXIT_FORECAST_NOT_COMMITTED"):
        observe_exit(migrated_settings, freeze=False)


def freeze_predictions(
    settings: Settings,
    registration: Any,
    source: str,
    observation: dict[str, Any],
    *,
    framework: bool = False,
    thesis_on_first: bool = False,
    after_first: Any = None,
) -> Any:
    """Seed independent original historical facts; one explicit test uses the full framework."""
    from datetime import datetime

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
    from stock_profiler.modules.evaluation.exit_contracts import ExitEvaluationReport

    previous = None
    latest = None
    for forecast in observation["forecasts"]:
        cutoff = datetime.fromisoformat(forecast["cutoff_at"]).replace(hour=16).isoformat()
        command = {
            "operation": "FREEZE",
            "synthetic": True,
            "generator_version": "fictional-exit/1",
            "seed": 3131,
            "cutoff_at": cutoff,
            "policy": None,
            "registration_event_id": registration.event_id,
            "source_standard_event_id": source,
            "previous_event_id": previous,
            "observations": [],
            "forecast_freezes": [
                {"lifecycle_id": observation["lifecycle_id"], "forecast": forecast}
            ],
        }
        if thesis_on_first and previous is None:
            command["theses"] = [
                {
                    "thesis_id": "fictional-original-thesis",
                    "version": "fictional-thesis/1",
                    "security_id": "synthetic-security-00",
                    "lifecycle_id": observation["lifecycle_id"],
                    "origin": "SYSTEM",
                    "registered_at": cutoff,
                    "valid_until": "2043-01-02T15:00:00Z",
                    "propositions": [
                        {
                            "proposition_id": "fictional-license",
                            "statement": "Fictional license remains valid.",
                            "metric": "licensed_operation_count",
                            "authority": "DISCLOSURE",
                            "source": "fictional-original-disclosure",
                            "operator": "LE",
                            "threshold": "0",
                            "trigger_direction": "LIQUIDATE",
                        }
                    ],
                }
            ]
        case = FrozenDecisionCase.model_validate(exit_case(settings, command))
        if framework:
            latest = run_frozen_decision_case(
                settings, case.model_dump(mode="json"), clock=GovernanceClock(cutoff)
            )
            assert latest.report is not None
            previous = latest.report.event_id
        else:
            assert case.exit_evaluation is not None
            result = case.expected_external_result.model_copy(
                update={
                    "exit_evaluation": ExitEvaluationReport(
                        disposition="FROZEN",
                        policy=registration.result.exit_evaluation.policy,
                        registration_event_id=registration.event_id,
                        cutoff_at=case.exit_evaluation.cutoff_at,
                        previous_event_id=previous,
                        forecast_freezes=case.exit_evaluation.forecast_freezes,
                    )
                }
            )
            ledger = DecisionLedger.from_settings(settings, clock=GovernanceClock(cutoff))
            ledger.persist_business_mapping_before_framework(case)
            with ledger.serialize_case_execution() as connection:
                fact = ledger.commit_event(
                    connection,
                    case=case,
                    framework_run_id=case.framework_run_id,
                    result=result,
                    stage_results=(),
                )
            previous = fact.decision_event_id
        if after_first and forecast is observation["forecasts"][0]:
            after_first(previous)
    return latest


def frozen_entry_fixture(settings: Settings) -> Any:
    registration = register_exit_policy(settings)
    payload = standard_entry_case(settings)
    payload["knowledge_cutoff"] = payload["report_generated_at"] = "2042-07-02T16:00:00Z"
    payload["standard_outcomes"]["cutoff_at"] = payload["knowledge_cutoff"]
    payload["standard_outcomes"]["observations"][0]["terminal"] = None
    bind_outcome_input(payload)
    source = run_frozen_decision_case(
        settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert source.report is not None and registration.report is not None
    assert source.report.result.standard_outcomes is not None
    identity = source.report.result.standard_outcomes.exit_positions[0].lifecycle_id
    observation = exit_observation(identity, end="2042-07-02T15:00:00Z")
    frozen = freeze_predictions(
        settings, registration.report, source.report.event_id, observation, framework=True
    )
    return frozen


def test_freeze_commits_original_prediction_at_its_current_market_cutoff(
    migrated_settings: Settings,
) -> None:
    frozen = frozen_entry_fixture(migrated_settings)
    assert frozen.report.result.exit_evaluation.disposition == "FROZEN"
    assert frozen.report.result.exit_evaluation.positions == ()
    assert (
        frozen.report.result.exit_evaluation.forecast_freezes[0].forecast.prediction_id
        == "fictional-prediction-0"
    )
    assert not frozen.report.result.exit_evaluation.authorization_granted


def test_market_correction_appends_version_and_prior_report_keeps_original_wealth(
    migrated_settings: Settings,
) -> None:

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.bootstrap.decision_cases import get_formal_report
    from stock_profiler.modules.delivery.access import AccessPrincipal

    original = observe_exit(migrated_settings)
    assert original.report is not None
    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.get_decision_event(original.report.event_id, connection)
    assert fact is not None and fact.case.exit_evaluation is not None
    command = fact.case.exit_evaluation.model_dump(mode="json")
    command["cutoff_at"] = "2042-07-09T16:00:00Z"
    command["previous_event_id"] = original.report.event_id
    observation = command["observations"][0]
    observation.update(
        evidence_id="fictional-market-correction",
        corrects_evidence_id="fictional-exit-path/1",
        correction_reason="Independent fictional price correction.",
    )
    for field in ("published_at", "acquired_at", "validated_at"):
        observation[field] = "2042-07-09T15:00:00Z"
    observation["sessions"][-1].update(price="12", vwap="12")
    corrected = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert corrected.report is not None and corrected.report.result.exit_evaluation is not None
    assert corrected.report.result.exit_evaluation.report_version == 2
    assert corrected.report.result.exit_evaluation.previous_report_id == original.report_version_id
    assert (
        corrected.report.result.exit_evaluation.positions[0].policies["PRIMARY"].daily_wealth[-1]
        == 1090
    )
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    assert (
        get_formal_report(original.report_version_id, migrated_settings, principal=principal)
        == original.report
    )
    foreign = AccessPrincipal(
        user_id="fictional-other-owner",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    assert (
        get_formal_report(original.report_version_id, migrated_settings, principal=foreign) is None
    )


def test_outcome_upload_cannot_change_registered_cost_curve(migrated_settings: Settings) -> None:
    def cheaper_after_outcome(observation: dict[str, Any]) -> None:
        observation["sessions"][1]["costs"]["minimum_commission"] = "0"

    with pytest.raises(ValueError, match="EXIT_COST_CURVE_CHANGED"):
        observe_exit(migrated_settings, mutate=cheaper_after_outcome)


def test_pending_lifecycle_keeps_due_labels_from_committed_unobserved_forecasts(
    migrated_settings: Settings,
) -> None:
    frozen = frozen_entry_fixture(migrated_settings)
    evidence = evaluate_observed(migrated_settings, frozen, cutoff="2042-08-08T16:00:00Z")
    assert evidence.counts.due == 0 and evidence.counts.immature == 1
    assert evidence.inference.disposition == "INDETERMINATE"
    assert any(
        reason.startswith("FORWARD_LABEL_MISSING:")
        for reason in evidence.inference.missing_evidence
    )
    assert len(evidence.positions[0].decisions) == 1


@pytest.mark.parametrize("measurement,accepted", [("0", True), ("1", False)])
def test_frozen_thesis_falsification_is_verified_before_shared_hard_exit(
    migrated_settings: Settings, measurement: str, accepted: bool
) -> None:
    registration = register_exit_policy(migrated_settings)
    payload = standard_entry_case(migrated_settings)
    payload["knowledge_cutoff"] = payload["report_generated_at"] = "2042-07-02T16:00:00Z"
    payload["standard_outcomes"]["cutoff_at"] = payload["knowledge_cutoff"]
    payload["standard_outcomes"]["observations"][0]["terminal"] = None
    bind_outcome_input(payload)
    source = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert (
        source.report is not None
        and source.report.result.standard_outcomes is not None
        and registration.report is not None
    )
    observation = exit_observation(
        source.report.result.standard_outcomes.exit_positions[0].lifecycle_id
    )

    def falsify(thesis_event_id: str) -> None:
        forecast = observation["forecasts"][1]
        fact = {
            "reason": "THESIS_FALSIFIED",
            "thesis_event_id": thesis_event_id,
            "evidence": {
                "evidence_id": "fictional-later-disclosure",
                "family": "RISK",
                "security_id": "synthetic-security-00",
                "source": "fictional-original-disclosure",
                "authority": "DISCLOSURE",
                "source_version": "fictional-disclosure/1",
                "public_at": forecast["cutoff_at"],
                "acquired_at": forecast["cutoff_at"],
                "validated_at": forecast["cutoff_at"],
                "valid_until": "2042-07-09T15:00:00Z",
                "complete": True,
                "metric": "licensed_operation_count",
                "value": measurement,
                "legal_termination": False,
            },
        }
        forecast.update(
            hard_exit=True, hard_evidence_id="fictional-later-disclosure", hard_evidence=fact
        )

    if not accepted:
        with pytest.raises(ValueError, match="EXIT_THESIS_NOT_FALSIFIED"):
            freeze_predictions(
                migrated_settings,
                registration.report,
                source.report.event_id,
                observation,
                framework=True,
                thesis_on_first=True,
                after_first=falsify,
            )
        return
    freeze_predictions(
        migrated_settings,
        registration.report,
        source.report.event_id,
        observation,
        framework=True,
        thesis_on_first=True,
        after_first=falsify,
    )
    command = {
        "operation": "OBSERVE",
        "synthetic": True,
        "generator_version": "fictional-exit/1",
        "seed": 3131,
        "cutoff_at": "2042-07-08T16:00:00Z",
        "policy": None,
        "registration_event_id": registration.report.event_id,
        "source_standard_event_id": source.report.event_id,
        "previous_event_id": None,
        "observations": [observation],
    }
    saved = run_frozen_decision_case(
        migrated_settings,
        exit_case(migrated_settings, command),
        clock=GovernanceClock(command["cutoff_at"]),
    )
    assert saved.report is not None and saved.report.result.exit_evaluation is not None
    position = saved.report.result.exit_evaluation.positions[0]
    assert not position.decisions[1].statistical
    for name in ("PRIMARY", "HARD_ONLY", "FIXED_LOSS", "TRAILING_DRAWDOWN"):
        assert position.policies[name].current_exposure == 0
    assert (
        position.policies["HARD_ONLY"].trades[0].signal_at.isoformat()
        == "2042-07-03T15:00:00+00:00"
    )
