"""Host-owned standard exit history at the existing frozen-case boundary."""

from datetime import datetime

from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
from stock_profiler.modules.decision_cases.ports import DecisionLedger, Transaction
from stock_profiler.modules.evaluation.contracts import StandardExitPosition
from stock_profiler.modules.evaluation.exit_contracts import ExitCounts, ExitEvaluationReport
from stock_profiler.modules.evaluation.exit_inference import summarize_exit_history
from stock_profiler.modules.evaluation.exit_paths import evaluate_exit_position
from stock_profiler.modules.evaluation.service import validate_evidence_revision
from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar


def assess_exit_evaluation(
    case: FrozenDecisionCase, ledger: DecisionLedger[Transaction], connection: Transaction
) -> ExitEvaluationReport:
    command = case.exit_evaluation
    assert command is not None and case.access_scope is not None
    history = ledger.exit_evaluation_history(connection, case.access_scope)
    if command.operation == "REGISTER":
        assert command.policy is not None
        if command.cutoff_at.strftime("%Y-%m") >= command.policy.start_month:
            raise ValueError("EXIT_POLICY_NOT_PREREGISTERED")
        if any(
            event.result.exit_evaluation
            and event.result.exit_evaluation.policy.version_id == command.policy.version_id
            for event in history
        ):
            raise ValueError("EXIT_POLICY_ALREADY_REGISTERED")
        return ExitEvaluationReport(
            disposition="REGISTERED",
            policy=command.policy,
            registration_event_id=None,
            cutoff_at=command.cutoff_at,
        )
    registration = next(
        (event for event in history if event.decision_event_id == command.registration_event_id),
        None,
    )
    if (
        registration is None
        or registration.case.exit_evaluation is None
        or registration.case.exit_evaluation.operation != "REGISTER"
        or datetime.fromisoformat(registration.committed_at) > command.cutoff_at
    ):
        raise ValueError("EXIT_REGISTRATION_UNAVAILABLE")
    report = registration.result.exit_evaluation
    assert report is not None
    policy = report.policy
    if command.seed != registration.case.exit_evaluation.seed:
        raise ValueError("EXIT_RESAMPLING_SEED_CHANGED")
    if command.operation == "EVALUATE":
        histories = tuple(
            event
            for event in history
            if event.case.exit_evaluation is not None
            and event.case.exit_evaluation.registration_event_id == command.registration_event_id
        )
        evaluations = tuple(
            event
            for event in histories
            if event.case.exit_evaluation and event.case.exit_evaluation.operation == "EVALUATE"
        )
        previous = evaluations[-1] if evaluations else None
        if command.previous_event_id != (previous.decision_event_id if previous else None):
            raise ValueError("EXIT_EVALUATION_LINEAGE_CONFLICT")
        if previous and datetime.fromisoformat(previous.committed_at) > command.cutoff_at:
            raise ValueError("EXIT_EVALUATION_CUTOFF_REVERSED")
        entries: dict[str, StandardExitPosition] = {}
        quantities = {}
        for event in ledger.standard_evaluation_history(connection, case.access_scope):
            standard = event.result.standard_outcomes
            source_command = event.case.standard_outcomes
            if (
                standard is None
                or source_command is None
                or standard.cutoff_at > command.cutoff_at
                or datetime.fromisoformat(event.committed_at) > command.cutoff_at
            ):
                continue
            if source_command.market_calendar_version != policy.market_calendar_version:
                continue
            for position in standard.exit_positions:
                month = position.entry_at.strftime("%Y-%m")
                if policy.start_month <= month <= policy.end_month:
                    entries[position.lifecycle_id] = position
                    quantities[position.lifecycle_id] = source_command.standard_quantity
        observations = {
            position.lifecycle_id: position.observation
            for event in histories
            if event.result.exit_evaluation
            and datetime.fromisoformat(event.committed_at) <= command.cutoff_at
            and event.result.exit_evaluation.cutoff_at <= command.cutoff_at
            for position in event.result.exit_evaluation.positions
            if position.observation is not None
        }
        evaluated_positions = tuple(
            evaluate_exit_position(
                position,
                observations.get(identity),
                policy,
                quantities[identity],
                command.cutoff_at,
            )
            for identity, position in entries.items()
        )
        due = sum(position.matures_at <= command.cutoff_at for position in evaluated_positions)
        evaluable = sum(position.state == "EVALUABLE" for position in evaluated_positions)
        counts = ExitCounts(
            registered=len(evaluated_positions),
            due=due,
            evaluable=evaluable,
            due_missing=due - evaluable,
            immature=len(evaluated_positions) - due,
        )
        prior_report = previous.result.exit_evaluation if previous else None
        formal = (
            ledger.get_formal_report_for_event(previous.decision_event_id, connection)
            if previous
            else None
        )
        inference = summarize_exit_history(
            evaluated_positions, policy, seed=command.seed, cutoff=command.cutoff_at
        )
        return ExitEvaluationReport(
            disposition=inference.disposition,
            policy=policy,
            registration_event_id=command.registration_event_id,
            cutoff_at=command.cutoff_at,
            previous_event_id=command.previous_event_id,
            previous_report_id=formal.report_version_id if formal else None,
            report_version=prior_report.report_version + 1 if prior_report else 1,
            positions=evaluated_positions,
            counts=counts,
            inference=inference,
        )
    source = ledger.get_decision_event(command.source_standard_event_id or "", connection)
    if (
        source is None
        or source.case.access_scope is None
        or not source.case.access_scope.same_scope_as(case.access_scope)
        or source.corrects_event_id
        or source.result.standard_outcomes is None
        or source.case.standard_outcomes is None
        or datetime.fromisoformat(source.committed_at) > command.cutoff_at
        or source.result.standard_outcomes.cutoff_at > command.cutoff_at
    ):
        raise ValueError("EXIT_STANDARD_SOURCE_UNAVAILABLE")
    standard = source.result.standard_outcomes
    if source.case.standard_outcomes.market_calendar_version != policy.market_calendar_version:
        raise ValueError("EXIT_STANDARD_CALENDAR_MISMATCH")
    frozen_events = tuple(
        event
        for event in history
        if event.case.exit_evaluation
        and event.case.exit_evaluation.operation == "FREEZE"
        and event.case.exit_evaluation.registration_event_id == command.registration_event_id
        and event.case.exit_evaluation.source_standard_event_id == command.source_standard_event_id
    )
    frozen_predictions = {
        (item.lifecycle_id, item.forecast.prediction_id): item.forecast
        for event in frozen_events
        if event.result.exit_evaluation
        for item in event.result.exit_evaluation.forecast_freezes
    }
    if command.operation == "FREEZE":
        previous = frozen_events[-1] if frozen_events else None
        if command.previous_event_id != (previous.decision_event_id if previous else None):
            raise ValueError("EXIT_FREEZE_LINEAGE_CONFLICT")
        if previous and datetime.fromisoformat(previous.committed_at) > command.cutoff_at:
            raise ValueError("EXIT_FREEZE_CUTOFF_REVERSED")
        calendar = synthetic_market_calendar(policy.market_calendar_version)
        assert calendar is not None
        session = calendar.session_at_or_before(command.cutoff_at)
        positions = {position.lifecycle_id: position for position in standard.exit_positions}
        for item in command.forecast_freezes:
            freeze_position = positions.get(item.lifecycle_id)
            if freeze_position is None:
                raise ValueError("EXIT_FORECAST_FOREIGN_LIFECYCLE")
            forecast = item.forecast
            if (
                session is None
                or forecast.cutoff_at != session.closed_at
                or not freeze_position.entry_at <= forecast.cutoff_at <= freeze_position.matures_at
                or forecast.model_version != policy.model_version
            ):
                raise ValueError("EXIT_FORECAST_FREEZE_CUTOFF_MISMATCH")
            if (item.lifecycle_id, forecast.prediction_id) in frozen_predictions or any(
                identity == item.lifecycle_id and prior.cutoff_at == forecast.cutoff_at
                for (identity, _), prior in frozen_predictions.items()
            ):
                raise ValueError("EXIT_FORECAST_ALREADY_FROZEN")
        if len(
            {(item.lifecycle_id, item.forecast.cutoff_at) for item in command.forecast_freezes}
        ) != len(command.forecast_freezes):
            raise ValueError("EXIT_FORECAST_IDENTITY_CONFLICT")
        return ExitEvaluationReport(
            disposition="FROZEN",
            policy=policy,
            registration_event_id=command.registration_event_id,
            cutoff_at=command.cutoff_at,
            previous_event_id=command.previous_event_id,
            forecast_freezes=command.forecast_freezes,
        )
    related = tuple(
        event
        for event in history
        if event.case.exit_evaluation
        and event.case.exit_evaluation.registration_event_id == command.registration_event_id
        and event.case.exit_evaluation.operation == "OBSERVE"
        and event.result.exit_evaluation is not None
        and any(
            position.lifecycle_id in {entry.lifecycle_id for entry in standard.exit_positions}
            for position in event.result.exit_evaluation.positions
            if event.result.exit_evaluation
        )
    )
    previous = related[-1] if related else None
    if command.previous_event_id != (previous.decision_event_id if previous else None):
        raise ValueError("EXIT_REPORT_LINEAGE_CONFLICT")
    previous_report = previous.result.exit_evaluation if previous else None
    retained = (
        {
            position.lifecycle_id: position.observation
            for position in previous_report.positions
            if position.observation is not None
        }
        if previous_report
        else {}
    )
    evidence_by_id = {
        position.observation.evidence_id: position.observation
        for event in related
        if event.result.exit_evaluation
        for position in event.result.exit_evaluation.positions
        if position.observation is not None
    }
    positions = {position.lifecycle_id: position for position in standard.exit_positions}
    if set(retained) - set(positions):
        raise ValueError("EXIT_POPULATION_MEMBER_REMOVAL")
    frozen_predictions = {
        (item.lifecycle_id, item.forecast.prediction_id): item.forecast
        for event in frozen_events
        if event.result.exit_evaluation
        and datetime.fromisoformat(event.committed_at) <= command.cutoff_at
        for item in event.result.exit_evaluation.forecast_freezes
    }
    for observation in command.observations:
        if observation.lifecycle_id not in positions:
            raise ValueError("EXIT_OBSERVATION_FOREIGN_LIFECYCLE")
        prior = retained.get(observation.lifecycle_id)
        validate_evidence_revision(observation, prior, evidence_by_id, command.cutoff_at)
        if prior is not None:
            prior_forecasts = {forecast.prediction_id: forecast for forecast in prior.forecasts}
            incoming_forecasts = {
                forecast.prediction_id: forecast for forecast in observation.forecasts
            }
            if any(
                incoming_forecasts.get(identity) != forecast
                for identity, forecast in prior_forecasts.items()
            ):
                raise ValueError("EXIT_FROZEN_FORECAST_CHANGED")
            if previous_report and any(
                forecast.cutoff_at <= previous_report.cutoff_at
                and forecast.prediction_id not in prior_forecasts
                for forecast in observation.forecasts
            ):
                raise ValueError("EXIT_FORECAST_BACKFILL_FORBIDDEN")
        if previous_report and command.cutoff_at < previous_report.cutoff_at:
            raise ValueError("EXIT_REPORT_CUTOFF_REVERSED")
        for forecast in observation.forecasts:
            key = (observation.lifecycle_id, forecast.prediction_id)
            if key not in frozen_predictions:
                raise ValueError("EXIT_FORECAST_NOT_COMMITTED")
            if frozen_predictions[key] != forecast:
                raise ValueError("EXIT_FROZEN_FORECAST_CHANGED")
        incoming_ids = {forecast.prediction_id for forecast in observation.forecasts}
        if any(
            identity == observation.lifecycle_id
            and prior.cutoff_at <= command.cutoff_at
            and prediction_id not in incoming_ids
            for (identity, prediction_id), prior in frozen_predictions.items()
        ):
            raise ValueError("EXIT_FROZEN_FORECAST_REMOVAL")
        retained[observation.lifecycle_id] = observation
    previous_formal = (
        ledger.get_formal_report_for_event(previous.decision_event_id, connection)
        if previous
        else None
    )
    return ExitEvaluationReport(
        disposition="OBSERVED",
        policy=policy,
        registration_event_id=command.registration_event_id,
        cutoff_at=command.cutoff_at,
        previous_event_id=command.previous_event_id,
        previous_report_id=previous_formal.report_version_id if previous_formal else None,
        report_version=previous_report.report_version + 1 if previous_report else 1,
        positions=tuple(
            evaluate_exit_position(
                position,
                retained.get(position.lifecycle_id),
                policy,
                source.case.standard_outcomes.standard_quantity,
                command.cutoff_at,
            )
            for position in positions.values()
        ),
    )
