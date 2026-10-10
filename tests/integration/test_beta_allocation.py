"""Synthetic permission and capacity contracts through committed frozen journeys."""

from copy import deepcopy
from typing import Any

import pytest
from synthetic_candidate_allocation import feasible_payload
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings


def beta_payload(
    settings: Settings,
    *,
    candidate_count: int = 1,
    cutoff_at: str = "2042-05-17T16:00:00Z",
    account_type: str = "SIMULATED_CASH",
) -> dict[str, Any]:
    payload, _ = feasible_payload(
        settings,
        candidate_count=candidate_count,
        cutoff_at=cutoff_at,
        qualification_valid_through="2042-05-23T15:00:00Z",
    )
    for name in (
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    ):
        payload["version_bundle"][name] = "candidate-allocation.2.0.0"
    command = payload["candidate_allocation"]
    command["contract_version"] = "2.0.0"
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger

    ledger = DecisionLedger.from_settings(settings)
    with ledger.serialize_case_execution() as connection:
        source = ledger.get_formal_report_for_event(command["candidate_event_id"], connection)
    assert source is not None and source.result.candidate_release is not None
    command["beta"] = {
        "synthetic": True,
        "generator_version": "fictional-beta-contract/1",
        "seed": 2819,
        "activation_id": "synthetic-beta-activation-2819",
        "requested_envelope": "INITIAL",
        "policy": {
            "version_id": "synthetic-beta-policy-2819",
            "initial_ratio": "0.03",
            "expanded_ratio": "0.06",
            "planned_month_count": 24,
            "clean_month_count": 3,
            "position_day_count": 20,
            "minimum_completion": "0.95",
            "statistical_age_domain": "synthetic-first-period",
            "statistical_probability_grid": "synthetic-downside-grid",
            "statistical_source": "SYNTHETIC_D0",
            "statistical_target": "synthetic-statistical-action",
            "statistical_purpose": "synthetic-first-action",
        },
        "qualification_bindings": [],
        "scope": {
            "capability": "B2",
            "purpose": "BETA_NEW_EXPOSURE",
            "evidence_level": "D0",
            "user_id": payload["access_scope"]["user_id"],
            "account_ids": payload["access_scope"]["account_ids"],
            "account_type": account_type,
            "source": "SYNTHETIC_D0",
            "market_state": source.result.candidate_release.market_state,
            "board": "SYNTHETIC",
            "target": "SYNTHETIC_BETA",
            "portfolio_scope": command["risk_handoff"]["portfolio_id"],
        },
    }
    payload["input"]["candidate_allocation"] = deepcopy(command)
    return payload


def test_missing_scoped_qualification_cannot_open_an_envelope(migrated_settings: Settings) -> None:
    execution = run_frozen_decision_case(
        migrated_settings, beta_payload(migrated_settings), clock=GovernanceClock()
    )
    assert execution.report is not None
    plan = execution.report.result.candidate_allocation
    assert plan is not None and plan.disposition == "BLOCKED"
    assert "BETA_QUALIFICATION_REQUIRED" in plan.reasons
    assert plan.total_principal == 0 and not plan.actionable


def test_unretained_qualification_identifiers_cannot_authorize(migrated_settings: Settings) -> None:
    from test_scoped_qualification import version

    payload = beta_payload(migrated_settings)
    payload["candidate_allocation"]["beta"]["qualification_bindings"] = [
        {
            "role": role,
            "decision_id": f"synthetic-missing-{role}",
            "version": version(migrated_settings),
        }
        for role in ("B0", "B1", "T0", "T1", "T2", "T3", "T4", "G1", "G2", "G3", "G4", "G5")
    ]
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.disposition == "BLOCKED"
    assert "BETA_QUALIFICATION_UNAVAILABLE" in report.result.candidate_allocation.reasons


def qualified_beta_payload(
    settings: Settings,
    *,
    candidate_count: int = 1,
    cutoff_at: str = "2042-05-17T16:00:00Z",
    invalid: tuple[str, str] | None = None,
    account_type: str = "SIMULATED_CASH",
) -> dict[str, Any]:
    from synthetic_candidate_allocation import qualified_prior_record, save_qualification_prior

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
    from stock_profiler.modules.qualification.contracts import QualificationScope

    payload = beta_payload(
        settings, candidate_count=candidate_count, cutoff_at=cutoff_at, account_type=account_type
    )
    case = FrozenDecisionCase.model_validate(payload)
    ledger = DecisionLedger.from_settings(settings)
    with ledger.serialize_case_execution() as connection:
        source = ledger.get_formal_report_for_event(
            payload["candidate_allocation"]["candidate_event_id"], connection
        )
    assert source is not None and source.result.candidate_release is not None
    original = qualified_prior_record(case, source.result.candidate_release)
    bindings = []
    for role in ("B0", "B1", "T0", "T1", "T2", "T3", "T4", "G1", "G2", "G3", "G4", "G5"):
        context = deepcopy(payload["candidate_allocation"]["beta"]["scope"])
        context["capability"] = role
        if role == "T4":
            policy = payload["candidate_allocation"]["beta"]["policy"]
            context.update(
                holding_age_domain=policy["statistical_age_domain"],
                probability_grid=policy["statistical_probability_grid"],
                source=policy["statistical_source"],
                target=policy["statistical_target"],
                purpose=policy["statistical_purpose"],
            )
        scope = QualificationScope.model_validate(context)
        from datetime import datetime

        earlier = datetime.fromisoformat("2042-01-31T16:00:00Z")
        evidence = original.evidence.model_copy(
            update={
                "scope": scope,
                "evidence_id": f"synthetic-beta-proof-{role}",
                "evaluation_end": earlier,
                "available_at": earlier,
            }
        )
        record = original.model_copy(
            update={
                "decision_id": f"synthetic-beta-qualification-{role}",
                "authorization_id": f"synthetic-beta-qualification-{role}",
                "scope": scope,
                "recorded_at": earlier,
                "evidence": evidence,
                "authorization_evidence": evidence,
                "formal_evidence": evidence,
                "formal_passing_evidence": evidence,
            }
        )
        if invalid is not None and invalid[0] == role:
            if invalid[1] in {"SUSPENDED", "REVOKED", "AT_RISK", "NOT_OBTAINED"}:
                record = record.model_copy(update={"status": invalid[1]})
            elif invalid[1] == "late_qualification":
                record = record.model_copy(
                    update={"recorded_at": datetime.fromisoformat("2042-05-17T16:00:00Z")}
                )
            elif invalid[1] == "missing_authorization":
                record = record.model_copy(update={"authorization_evidence": None})
            elif invalid[1] == "basis_scope":
                wrong_scope = scope.model_copy(update={"source": "synthetic-other-basis"})
                wrong = evidence.model_copy(update={"scope": wrong_scope})
                record = record.model_copy(update={"evidence": wrong})
            elif invalid[1] == "expired":
                from datetime import datetime

                evidence = evidence.model_copy(
                    update={"expires_at": datetime.fromisoformat("2042-05-17T15:00:00Z")}
                )
                record = record.model_copy(
                    update={
                        "evidence": evidence,
                        "authorization_evidence": evidence,
                        "formal_evidence": evidence,
                        "formal_passing_evidence": evidence,
                    }
                )
        save_qualification_prior(settings, case, record)
        bindings.append(
            {
                "role": role,
                "decision_id": record.decision_id,
                "version": record.version.model_dump(mode="json"),
            }
        )
    payload["candidate_allocation"]["beta"]["qualification_bindings"] = bindings
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    return payload


def test_initial_envelope_is_a_total_capacity_input(migrated_settings: Settings) -> None:
    payload = qualified_beta_payload(migrated_settings)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.disposition == "PLANNED"
    assert plan.total_principal == 300
    assert plan.rows[0].principal == 300
    assert "BETA_CAPACITY_EXHAUSTED" in plan.rows[0].reasons
    assert not plan.actionable


def test_requested_expansion_without_real_observation_stays_at_initial_capacity(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    payload["candidate_allocation"]["beta"]["requested_envelope"] = "EXPANDED"
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == 300
    assert plan.beta is not None and plan.beta.permission_envelope == "INITIAL"
    assert "BETA_EXPANSION_OBSERVATION_REQUIRED" in plan.beta.reasons


def expansion_observations(payload: dict[str, Any]) -> dict[str, Any]:
    from datetime import UTC, datetime

    from stock_profiler.modules.portfolio.market_calendar import synthetic_market_calendar

    beta = payload["candidate_allocation"]["beta"]
    calendar = synthetic_market_calendar("synthetic-calendar-v1")
    assert calendar is not None
    days = calendar.recent_completed_sessions(
        datetime.fromisoformat(payload["knowledge_cutoff"]), 20
    )
    months = [f"{2040 + (4 + i) // 12:04d}-{(4 + i) % 12 + 1:02d}" for i in range(24)]
    month_closes = {}
    for month in months:
        year, number = map(int, month.split("-"))
        boundary = datetime(year + number // 12, number % 12 + 1, 1, tzinfo=UTC)
        month_closes[month] = (
            calendar.recent_completed_sessions(boundary, 1)[0]
            .closed_at.isoformat()
            .replace("+00:00", "Z")
        )

    from stock_profiler.modules.qualification.beta_contracts import BetaObservations

    result = {
        "synthetic": True,
        "generator_version": "fictional-observations/1",
        "seed": 2819,
        "scope": deepcopy(beta["scope"]),
        "activation_id": beta["activation_id"],
        "policy_version": beta["policy"]["version_id"],
        "qualification_bindings": deepcopy(beta["qualification_bindings"]),
        "simulated_origin": "REAL_TIME",
        "activated_at": "2042-02-01T00:00:00Z",
        "registered_at": "2040-04-01T00:00:00Z",
        "available_at": payload["knowledge_cutoff"],
        "plan_months": months,
        "months": [
            {
                "plan_month": month,
                "closed_at": month_closes[month],
                "batch_required": 1,
                "batch_completed": 1,
                "timely_required": 1,
                "timely_completed": 1,
                "data_completed": True,
                "pipeline_completed": True,
                "plan_required": 1,
                "plan_completed": 1,
                "notification_required": 1,
                "notification_completed": 1,
                "report_required": 1,
                "report_completed": 1,
                "confirmation_required": 1,
                "confirmation_completed": 1,
                "reconciliation_required": 1,
                "reconciliation_completed": 1,
                "safety_failures": [],
            }
            for month in months
        ],
        "market_calendar_version": "synthetic-calendar-v1",
        "plan_days": [session.closed_at.isoformat().replace("+00:00", "Z") for session in days],
        "days": [
            {
                "closed_at": session.closed_at.isoformat().replace("+00:00", "Z"),
                "data_completed": True,
                "pipeline_completed": True,
                "system_position_ids": ["synthetic-position-2819"],
                "p0p1_missing": 0,
                "safety_failures": [],
            }
            for session in days
        ],
        "terminal_plans": [
            {
                "plan_id": "synthetic-terminal-plan-2819",
                "confirmation_id": "synthetic-terminal-confirmation-2819",
                "terminal_outcome": "FULLY_FILLED",
                "broker_order_ids": ["synthetic-order-2819"],
                "fill_ids": ["synthetic-fill-2819"],
                "funds_reconciliation_id": "synthetic-funds-2819",
                "position_reconciliation_id": "synthetic-position-2819",
                "accepted_intents": 1,
                "remaining_reservation": "0",
                "unknown_orders": [],
                "execution_deviations_reconciled": True,
                "authoritative": True,
                "closed_at": "2042-05-16T16:00:00Z",
            }
        ],
        "expansion_confirmed": True,
        "effective_at": payload["knowledge_cutoff"],
    }

    return BetaObservations.model_validate(result).model_dump(mode="json")


def test_expansion_requires_conjunctive_observations_and_an_explicit_new_scope(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["requested_envelope"] = "EXPANDED"
    beta["observations"] = expansion_observations(payload)
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == 600
    assert plan.beta is not None and plan.beta.permission_envelope == "EXPANDED"
    assert plan.beta.operations is not None and plan.beta.operations.window_complete
    assert not plan.beta.actionable


@pytest.mark.parametrize(
    "role", ["B0", "B1", "T0", "T1", "T2", "T3", "T4", "G1", "G2", "G3", "G4", "G5"]
)
def test_every_dependency_is_required(migrated_settings: Settings, role: str) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["qualification_bindings"] = [
        item for item in beta["qualification_bindings"] if item["role"] != role
    ]
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 0
    assert report.result.candidate_allocation.reasons == ("BETA_QUALIFICATION_REQUIRED",)


@pytest.mark.parametrize(
    "change",
    [
        "history",
        "replay",
        "poc",
        "scope",
        "policy",
        "activation",
        "binding",
        "future",
        "late_registration",
        "confirmation",
        "missing_day",
        "no_position",
        "p0p1",
        "unknown",
        "reservation",
        "funds",
        "non_authoritative",
        "terminal",
        "clean_month",
        "calendar",
    ],
)
def test_expansion_rejects_each_missing_observation(
    migrated_settings: Settings, change: str
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["requested_envelope"] = "EXPANDED"
    obs = expansion_observations(payload)
    if change in {"history", "replay", "poc"}:
        obs["simulated_origin"] = {"history": "HISTORICAL", "replay": "REPLAY", "poc": "POC"}[
            change
        ]
    elif change == "scope":
        obs["scope"]["source"] = "synthetic-other-source"
    elif change == "policy":
        obs["policy_version"] = "synthetic-other-policy"
    elif change == "activation":
        obs["activation_id"] = "synthetic-other-activation"
    elif change == "binding":
        obs["qualification_bindings"][0]["decision_id"] = "synthetic-other-binding"
    elif change == "future":
        obs["available_at"] = "2042-05-18T16:00:00Z"
    elif change == "late_registration":
        obs["registered_at"] = "2042-05-01T00:00:00Z"
    elif change == "confirmation":
        obs["expansion_confirmed"] = False
    elif change == "missing_day":
        obs["days"].pop(0)
    elif change == "no_position":
        obs["days"][0]["system_position_ids"] = []
    elif change == "p0p1":
        obs["days"][0]["p0p1_missing"] = 1
    elif change == "unknown":
        obs["terminal_plans"][0]["unknown_orders"] = ["synthetic-unknown"]
    elif change == "reservation":
        obs["terminal_plans"][0]["remaining_reservation"] = "1"
    elif change == "funds":
        obs["terminal_plans"][0]["funds_reconciliation_id"] = None
    elif change == "non_authoritative":
        obs["terminal_plans"][0]["authoritative"] = False
    elif change == "terminal":
        obs["terminal_plans"] = []
    elif change == "clean_month":
        obs["months"][-1]["plan_completed"] = 0
    elif change == "calendar":
        obs["market_calendar_version"] = "synthetic-unknown-calendar"
    beta["observations"] = obs
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal <= 300
    assert plan.beta is not None and plan.beta.permission_envelope == "INITIAL"
    assert plan.beta.reasons and not plan.beta.actionable


@pytest.mark.parametrize(
    "change",
    ["missing_month", "duplicate_month", "missing_plan_month", "consecutive_core", "safety"],
)
def test_operational_failures_close_new_capacity(migrated_settings: Settings, change: str) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    obs = expansion_observations(payload)
    if change == "missing_month":
        obs["months"].pop(0)
    elif change == "duplicate_month":
        obs["months"].append(deepcopy(obs["months"][0]))
    elif change == "missing_plan_month":
        obs["plan_months"].pop(0)
    elif change == "consecutive_core":
        for item in obs["months"]:
            item["report_required"] = item["report_completed"] = 100
        obs["months"][0]["report_completed"] = 99
        obs["months"][1]["report_completed"] = 99
    elif change == "safety":
        obs["months"][0]["safety_failures"] = ["SYNTHETIC_ATOMICITY_FAILURE"]
    beta["observations"] = obs
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == 0
    assert plan.beta is not None and "BETA_OPERATIONAL_GATE_FAILED" in plan.beta.reasons


def test_no_trade_keeps_zero_denominators_without_manufacturing_fills(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["requested_envelope"] = "EXPANDED"
    obs = expansion_observations(payload)
    for month in obs["months"]:
        for name in ("confirmation", "reconciliation"):
            month[f"{name}_required"] = month[f"{name}_completed"] = 0
    terminal = obs["terminal_plans"][0]
    terminal.update(
        terminal_outcome="NO_TRADE", broker_order_ids=[], fill_ids=[], accepted_intents=0
    )
    for day in obs["days"]:
        day["system_position_ids"] = []
    beta["observations"] = obs
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == 300
    assert plan.beta is not None and plan.beta.operations is not None
    assert plan.beta.permission_envelope == "INITIAL"
    for name in ("confirmation", "reconciliation"):
        metric = plan.beta.operations.metrics[name]
        assert metric.required == 0 and metric.rate is None and metric.status == "NOT_APPLICABLE"


def test_completion_boundary_is_independent_of_safety_tolerance(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    obs = expansion_observations(payload)
    obs["months"][0].update(notification_required=200, notification_completed=180)
    for month in obs["months"][1:]:
        month.update(notification_required=0, notification_completed=0)
    # Exactly 95% is admissible; below it is not. No two adjacent core failures.
    obs["months"][0]["notification_completed"] = 190
    beta["observations"] = obs
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert first is not None and first.result.candidate_allocation is not None
    assert first.result.candidate_allocation.total_principal == 300
    payload["business_identity"] += ":below-boundary"
    obs["months"][0]["notification_completed"] = 189
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    second = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert second is not None and second.result.candidate_allocation is not None
    assert second.result.candidate_allocation.total_principal == 0


@pytest.mark.parametrize(
    "status",
    [
        "SUSPENDED",
        "REVOKED",
        "AT_RISK",
        "NOT_OBTAINED",
        "expired",
        "missing_authorization",
        "basis_scope",
    ],
)
def test_invalid_retained_qualification_fails_closed(
    migrated_settings: Settings, status: str
) -> None:
    payload = qualified_beta_payload(migrated_settings, invalid=("T4", status))
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 0
    assert "BETA_QUALIFICATION_NOT_CURRENT" in report.result.candidate_allocation.reasons


@pytest.mark.parametrize(
    "field",
    [
        "user_id",
        "account_ids",
        "source",
        "market_state",
        "board",
        "account_type",
        "target",
        "purpose",
        "capability",
    ],
)
def test_qualification_cannot_move_to_a_different_scope(
    migrated_settings: Settings, field: str
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    context = payload["candidate_allocation"]["beta"]["scope"]
    context[field] = (
        ["synthetic-foreign-account"] if field == "account_ids" else "synthetic-foreign-scope"
    )
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 0


def beta_confirmation_payload(settings: Settings) -> dict[str, Any]:
    payload = qualified_beta_payload(settings, candidate_count=2, cutoff_at="2042-05-19T16:00:00Z")
    report = run_frozen_decision_case(
        settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == 300
    allocation = payload.pop("candidate_allocation")
    payload["input"].pop("candidate_allocation")
    payload["business_identity"] = "synthetic-beta-confirmation:first"
    payload["version_bundle"].update(
        case_contract_version="candidate-confirmation.1.0.0",
        host_contract_version="candidate-confirmation.1.0.0",
        report_projection_contract_version="candidate-confirmation.1.0.0",
    )
    payload["candidate_confirmation"] = {
        "contract_version": "1.0.0",
        "operation": "SUBMIT",
        "user_id": payload["access_scope"]["user_id"],
        "portfolio_id": allocation["risk_handoff"]["portfolio_id"],
        "candidate_batch_id": plan.candidate_batch_id,
        "plan_event_id": report.event_id,
        "plan_id": plan.plan_id,
        "seen_confirmation_id": None,
        "idempotency_key": "first",
        "withdrawal_position_event_id": None,
        "choices": [
            {"security_id": row.candidate.security_id, "choice": "ACCEPT"}
            for row in plan.rows
            if row.principal > 0
        ],
        "revalidation": allocation,
    }
    payload["input"]["candidate_confirmation"] = deepcopy(payload["candidate_confirmation"])
    return payload


def test_beta_confirmation_reserves_the_whole_vector_atomically(
    migrated_settings: Settings,
) -> None:
    payload = beta_confirmation_payload(migrated_settings)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    assert first.report is not None and first.report.result.candidate_confirmation is not None
    confirmation = first.report.result.candidate_confirmation
    assert confirmation.disposition == "CONFIRMED"
    assert sum(item.principal for item in confirmation.reservations) == 300
    replay = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    assert replay.report == first.report and replay.framework_run_id == first.framework_run_id


def test_confirmation_cannot_remove_a_saved_beta_binding(migrated_settings: Settings) -> None:
    payload = beta_confirmation_payload(migrated_settings)
    revalidation = payload["candidate_confirmation"]["revalidation"]
    revalidation.pop("beta")
    revalidation["contract_version"] = "1.0.0"
    # Keep the ordinary capacity coincidentally equal, so identity remains decisive.
    revalidation["policy"]["entry_target_ratio"] = "0.015"
    payload["input"]["candidate_confirmation"] = deepcopy(payload["candidate_confirmation"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "BLOCKED"
    assert report.result.candidate_confirmation.reservations == ()
    assert "BETA_BINDING_CHANGED" in report.result.candidate_confirmation.reasons


def test_filled_beta_exposure_and_retained_orders_share_one_total_envelope(
    migrated_settings: Settings,
) -> None:
    from test_candidate_execution import broker_payload, forward_allocation_payload

    payload, _ = broker_payload(
        migrated_settings,
        quantity="10",
        price="10",
        confirmation_case=beta_confirmation_payload(migrated_settings),
    )
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert execution is not None and execution.result.candidate_execution is not None
    assert execution.result.candidate_execution.fills[0].classification == "PLANNED"
    later = forward_allocation_payload(migrated_settings, payload)
    # Changing the activation label cannot erase acquired exposure or reservations.
    later["candidate_allocation"]["beta"]["activation_id"] += ":different-label"
    later["input"]["candidate_allocation"] = deepcopy(later["candidate_allocation"])
    report = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == 0
    assert plan.beta is not None and plan.beta.committed_exposure == 300
    assert sum(item.principal for item in plan.commitments) == 200
    assert plan.rows[0].candidate.candidate


def test_linked_excess_fill_is_retained_as_acquired_beta_exposure(
    migrated_settings: Settings,
) -> None:
    from test_candidate_execution import (
        broker_payload,
        forward_allocation_payload,
        freeze_execution,
    )

    payload, _ = broker_payload(
        migrated_settings,
        quantity="30",
        price="10",
        terminal=True,
        confirmation_case=beta_confirmation_payload(migrated_settings),
    )
    payload["candidate_execution"]["orders"][0]["quantity"] = "30"
    freeze_execution(payload)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert execution is not None and execution.result.candidate_execution is not None
    assert execution.result.candidate_execution.fills, execution.result.candidate_execution.reasons
    assert execution.result.candidate_execution.fills[0].external_quantity == 10
    later = forward_allocation_payload(migrated_settings, payload)
    report = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock(payload["knowledge_cutoff"])
    ).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.beta is not None and plan.beta.committed_exposure == 400
    assert plan.total_principal == 0


def suspend_beta_dependency(settings: Settings, payload: dict[str, Any]) -> None:
    from datetime import datetime

    from synthetic_candidate_allocation import save_qualification_prior

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase

    case = FrozenDecisionCase.model_validate(payload)
    assert case.access_scope is not None
    ledger = DecisionLedger.from_settings(settings)
    with ledger.serialize_case_execution() as connection:
        records = ledger.governance_history(connection, case.access_scope)
    prior = next(
        item.qualification
        for item in records
        if item.qualification is not None and item.qualification.scope.capability == "T2"
    )
    assert prior is not None
    record = prior.model_copy(
        update={
            "decision_id": "synthetic-beta-suspended",
            "previous_decision_id": prior.decision_id,
            "status": "SUSPENDED",
            "recorded_at": datetime.fromisoformat("2042-05-19T16:01:00Z"),
        }
    )
    save_qualification_prior(settings, case, record)


def test_workspace_stops_permission_after_qualification_changes_and_keeps_saved_candidates(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.modules.delivery.access import AccessPrincipal

    payload = beta_confirmation_payload(migrated_settings)
    reader = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    before = delivery.candidate_workspace(reader)
    assert before is not None and before.allocations[0].new_actions_permitted
    suspend_beta_dependency(migrated_settings, payload)
    after = delivery.candidate_workspace(reader)
    assert after is not None and not after.allocations[0].new_actions_permitted
    assert "BETA_QUALIFICATION_NOT_CURRENT" in after.allocations[0].stop_reasons
    assert after.releases == before.releases
    assert after.allocations[0].plan_report == before.allocations[0].plan_report
    assert after.allocations[0].allocation.beta == before.allocations[0].allocation.beta
    blocked = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert blocked is not None and blocked.result.candidate_confirmation is not None
    assert blocked.result.candidate_confirmation.reservations == ()


def test_shadow_beta_case_never_opens_user_permission(migrated_settings: Settings) -> None:
    payload = qualified_beta_payload(migrated_settings)
    payload["access_scope"]["visibility"] = "SHADOW"
    payload["business_identity"] += ":shadow"
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is None and execution.publication_status == "CLOSED"


def test_post_cutoff_observations_cannot_expand_frozen_capacity(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["requested_envelope"] = "EXPANDED"
    obs = expansion_observations(payload)
    obs.update(available_at="2042-05-17T16:00:30Z", effective_at="2042-05-17T16:00:40Z")
    beta["observations"] = obs
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 300


@pytest.mark.parametrize(("scenario", "amount"), [("full", 700), ("partial", 300), ("zero", 0)])
def test_capacity_matrix_preserves_standard_candidates(
    migrated_settings: Settings, scenario: str, amount: int
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    command = payload["candidate_allocation"]
    if scenario == "full":
        command["beta"]["policy"].update(initial_ratio="0.2", expanded_ratio="0.3")
    elif scenario == "zero":
        command["routes"][0]["permission"] = False
    payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    assert plan.total_principal == amount
    assert plan.rows[0].candidate.candidate
    assert (
        plan.rows[0].outcome
        == {"full": "FULLY_ALLOCATED", "partial": "PARTIALLY_ALLOCATED", "zero": "UNALLOCATED"}[
            scenario
        ]
    )


def test_beta_unknown_commit_retains_original_identity_and_prevents_new_capacity(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.ports import DecisionEventCommitUncertainError

    payload = beta_confirmation_payload(migrated_settings)

    def fail_commit(self: DecisionLedger, connection: object, **kwargs: object) -> None:
        raise DecisionEventCommitUncertainError("synthetic uncertain beta storage")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)
        unknown = run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
        )
    assert unknown.business_commit_status == "UNKNOWN" and unknown.report is None
    other = deepcopy(payload)
    other["candidate_confirmation"]["idempotency_key"] = "different-device"
    other["input"]["candidate_confirmation"] = deepcopy(other["candidate_confirmation"])
    blocked = run_frozen_decision_case(
        migrated_settings, other, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert blocked is not None and blocked.result.candidate_confirmation is not None
    assert blocked.result.candidate_confirmation.reservations == ()
    replay = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    assert replay.framework_run_id == unknown.framework_run_id
    assert replay.report is not None and replay.report.result.candidate_confirmation is not None
    assert (
        sum(item.principal for item in replay.report.result.candidate_confirmation.reservations)
        == 300
    )


def test_beta_concurrent_devices_cannot_split_or_duplicate_reservations(
    migrated_settings: Settings,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    payload = beta_confirmation_payload(migrated_settings)
    other = deepcopy(payload)
    other["candidate_confirmation"]["idempotency_key"] = "concurrent-device"
    other["candidate_confirmation"]["choices"][1]["choice"] = "DEFER"
    other["input"]["candidate_confirmation"] = deepcopy(other["candidate_confirmation"])

    def submit(request: dict[str, Any]) -> Any:
        return run_frozen_decision_case(
            migrated_settings, request, clock=GovernanceClock("2042-05-19T16:01:00Z")
        ).report

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(submit, (payload, other)))
    assert all(report is not None for report in reports)
    outcomes = [report.result.candidate_confirmation for report in reports]
    assert sorted(outcome.disposition for outcome in outcomes) == ["BLOCKED", "CONFIRMED"]
    assert sum(item.principal for outcome in outcomes for item in outcome.reservations) <= 300


@pytest.mark.parametrize("unknown", [False, True])
def test_beta_withdrawal_requires_authoritative_order_exclusion_even_after_suspension(
    migrated_settings: Settings, unknown: bool
) -> None:
    from test_candidate_confirmation import save_withdrawal_proof

    payload = beta_confirmation_payload(migrated_settings)
    accepted = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert accepted is not None and accepted.result.candidate_confirmation is not None
    suspend_beta_dependency(migrated_settings, payload)
    proof = save_withdrawal_proof(migrated_settings, payload, open_order=unknown, unknown=unknown)
    command = payload["candidate_confirmation"]
    command.update(
        operation="WITHDRAW",
        seen_confirmation_id=accepted.event_id,
        idempotency_key="beta-withdrawal",
        withdrawal_position_event_id=proof,
    )
    for choice in command["choices"]:
        choice["choice"] = "DECLINE"
    payload["input"]["candidate_confirmation"] = deepcopy(command)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:02:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    outcome = report.result.candidate_confirmation
    if unknown:
        assert outcome.disposition == "BLOCKED" and not outcome.released_reservation_ids
    else:
        assert outcome.disposition == "CONFIRMED" and not outcome.reservations
        assert len(outcome.released_reservation_ids) == len(
            accepted.result.candidate_confirmation.reservations
        )


def test_later_case_cannot_erase_a_saved_operational_failure(migrated_settings: Settings) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["observations"] = expansion_observations(payload)
    beta["observations"]["months"][0]["safety_failures"] = ["SYNTHETIC_CAPACITY_CAUSALITY_FAILURE"]
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    failure = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert failure is not None and failure.result.candidate_allocation is not None
    assert failure.result.candidate_allocation.total_principal == 0
    payload["business_identity"] += ":omitted-observation"
    beta.pop("observations")
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    later = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert later is not None and later.result.candidate_allocation is not None
    assert later.result.candidate_allocation.total_principal == 0
    assert "BETA_OPERATIONAL_HISTORY_REQUIRED" in later.result.candidate_allocation.reasons


def test_no_trade_terminal_needs_only_applicable_reconciliation(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["requested_envelope"] = "EXPANDED"
    obs = expansion_observations(payload)
    terminal = obs["terminal_plans"][0]
    terminal.update(
        terminal_outcome="ALL_DECLINED",
        accepted_intents=0,
        broker_order_ids=[],
        fill_ids=[],
        funds_reconciliation_id=None,
        position_reconciliation_id=None,
    )
    beta["observations"] = obs
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 300
    assert report.result.candidate_allocation.beta is not None
    assert report.result.candidate_allocation.beta.permission_envelope == "INITIAL"


def test_position_delivery_allows_one_failure_but_never_a_missing_p0p1(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["requested_envelope"] = "EXPANDED"
    obs = expansion_observations(payload)
    obs["days"][0]["pipeline_completed"] = False
    beta["observations"] = obs
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 600


def test_daily_audit_uses_all_twenty_planned_days(migrated_settings: Settings) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["observations"] = expansion_observations(payload)
    beta["observations"]["days"][0]["pipeline_completed"] = False
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    result = report.result.candidate_allocation.beta
    assert result is not None and result.operations is not None
    metric = result.operations.metrics["daily_pipeline"]
    assert metric.required == 20 and metric.completed == 19 and str(metric.rate) == "0.95"


def test_tolerated_daily_failure_can_age_out_without_erasing_its_report(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings, cutoff_at="2042-05-19T16:00:00Z")
    beta = payload["candidate_allocation"]["beta"]
    beta["observations"] = expansion_observations(payload)
    beta["observations"]["days"][0]["pipeline_completed"] = False
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert original is not None and original.result.candidate_allocation is not None
    assert original.result.candidate_allocation.total_principal == 300
    from test_candidate_execution import forward_allocation_payload
    from test_position_state_reconciliation import (
        position_case_payload,
        refresh_current_position_evidence,
    )

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger

    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        risk = ledger.get_decision_event(
            payload["candidate_allocation"]["risk_handoff"]["concentration_event_id"], connection
        )
    assert risk is not None and risk.case.concentration is not None
    snapshot = risk.case.concentration.position_snapshot.model_dump(mode="json")
    snapshot.update(
        snapshot_id="synthetic-rolled-window-position", cutoff_at="2042-05-20T16:00:00Z"
    )
    refresh_current_position_evidence(snapshot, snapshot["cutoff_at"])
    proof = run_frozen_decision_case(
        migrated_settings,
        position_case_payload(migrated_settings, "rolled-window", snapshot),
        clock=GovernanceClock(snapshot["cutoff_at"]),
    ).report
    assert proof is not None
    later = forward_allocation_payload(
        migrated_settings,
        {
            "candidate_execution": {
                "plan_event_id": original.event_id,
                "position_event_id": proof.event_id,
                "cutoff_at": snapshot["cutoff_at"],
            }
        },
    )
    later["candidate_allocation"]["beta"]["observations"] = expansion_observations(later)
    from synthetic_candidate_allocation import return_market_dates

    correlations = later["candidate_allocation"]["correlations"]
    correlations["market_dates"] = return_market_dates(
        snapshot["cutoff_at"], correlations["market_calendar_version"]
    )
    later["input"]["candidate_allocation"] = deepcopy(later["candidate_allocation"])
    updated = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-20T16:01:00Z")
    ).report
    assert updated is not None and updated.result.candidate_allocation is not None
    assert updated.result.candidate_allocation.total_principal == 300, (
        updated.result.candidate_allocation.reasons,
        updated.result.candidate_allocation.rows,
    )
    with ledger.serialize_case_execution() as connection:
        retained = ledger.get_formal_report_for_event(original.event_id, connection)
    assert retained == original


def test_month_end_before_activation_is_not_a_clean_beta_month(migrated_settings: Settings) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["requested_envelope"] = "EXPANDED"
    obs = expansion_observations(payload)
    obs["activated_at"] = "2042-02-28T23:59:00Z"
    beta["observations"] = obs
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 300


def test_observation_before_qualification_cannot_authorize_expansion(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings, invalid=("T2", "late_qualification"))
    beta = payload["candidate_allocation"]["beta"]
    beta["requested_envelope"] = "EXPANDED"
    beta["observations"] = expansion_observations(payload)
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 300


def test_synthetic_marker_cannot_be_a_coerced_number(migrated_settings: Settings) -> None:
    payload = beta_payload(migrated_settings)
    payload["candidate_allocation"]["beta"]["synthetic"] = 1
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    with pytest.raises(ValueError, match="synthetic"):
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())


def test_other_account_type_qualification_does_not_cover_the_saved_portfolio(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings, account_type="SIMULATED_MARGIN")
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 0


def test_same_policy_version_cannot_silently_raise_coverage(migrated_settings: Settings) -> None:
    payload = qualified_beta_payload(migrated_settings)
    initial = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert initial is not None and initial.result.candidate_allocation is not None
    assert initial.result.candidate_allocation.total_principal == 300
    payload["business_identity"] += ":changed-policy"
    payload["candidate_allocation"]["beta"]["policy"].update(
        initial_ratio="0.06", expanded_ratio="0.09"
    )
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    later = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert later is not None and later.result.candidate_allocation is not None
    assert later.result.candidate_allocation.total_principal == 0
    assert "BETA_POLICY_VERSION_CONFLICT" in later.result.candidate_allocation.reasons


def test_relabeling_policy_without_new_qualification_cannot_raise_coverage(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    payload["business_identity"] += ":relabeled-policy"
    payload["candidate_allocation"]["beta"]["policy"].update(
        version_id="synthetic-other-policy", initial_ratio="0.06", expanded_ratio="0.09"
    )
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    later = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert later is not None and later.result.candidate_allocation is not None
    assert later.result.candidate_allocation.total_principal == 0


def test_missing_p0p1_closes_initial_capacity_and_cannot_be_omitted(
    migrated_settings: Settings,
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["observations"] = expansion_observations(payload)
    beta["observations"]["days"][0]["p0p1_missing"] = 1
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    first = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert first is not None and first.result.candidate_allocation is not None
    assert first.result.candidate_allocation.total_principal == 0
    payload["business_identity"] += ":omitted-day-failure"
    beta.pop("observations")
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    later = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert later is not None and later.result.candidate_allocation is not None
    assert later.result.candidate_allocation.total_principal == 0


@pytest.mark.parametrize("relabel", ["activation_id", "simulated_origin"])
def test_relabeling_observation_cannot_bypass_saved_safety_failure(
    migrated_settings: Settings, relabel: str
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["observations"] = expansion_observations(payload)
    beta["observations"]["months"][0]["safety_failures"] = ["SYNTHETIC_ATOMICITY_FAILURE"]
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    payload["business_identity"] += ":relabel"
    beta["observations"][relabel] = "REPLAY" if relabel == "simulated_origin" else "other"
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    later = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert later is not None and later.result.candidate_allocation is not None
    assert later.result.candidate_allocation.total_principal == 0


def test_failed_daily_window_closes_initial_coverage(migrated_settings: Settings) -> None:
    payload = qualified_beta_payload(migrated_settings)
    beta = payload["candidate_allocation"]["beta"]
    beta["observations"] = expansion_observations(payload)
    for day in beta["observations"]["days"]:
        day["pipeline_completed"] = False
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 0


def test_saved_plan_permission_observes_a_later_scope_safety_failure(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.modules.delivery.access import AccessPrincipal

    payload = qualified_beta_payload(migrated_settings, cutoff_at="2042-05-19T16:00:00Z")
    clock = GovernanceClock("2042-05-19T16:01:00Z")
    run_frozen_decision_case(migrated_settings, payload, clock=clock)
    reader = AccessPrincipal(
        user_id=payload["access_scope"]["user_id"],
        account_ids=tuple(payload["access_scope"]["account_ids"]),
        permissions=("REPORT_READ", "CANDIDATE_ALLOCATION", "CANDIDATE_COMMAND"),
    )
    delivery = ResultDelivery.from_settings(migrated_settings, clock=clock)
    before = delivery.candidate_workspace(reader)
    assert before is not None and before.allocations[0].new_actions_permitted
    payload["business_identity"] += ":new-safety-failure"
    beta = payload["candidate_allocation"]["beta"]
    beta["observations"] = expansion_observations(payload)
    beta["observations"]["months"][0]["safety_failures"] = ["SYNTHETIC_IDENTITY_FAILURE"]
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    run_frozen_decision_case(migrated_settings, payload, clock=clock)
    after = delivery.candidate_workspace(reader)
    assert after is not None and not after.allocations[0].new_actions_permitted
    assert after.allocations[0].plan_report == before.allocations[0].plan_report


@pytest.mark.parametrize(
    "field",
    [
        "statistical_age_domain",
        "statistical_probability_grid",
        "statistical_source",
        "statistical_target",
        "statistical_purpose",
    ],
)
def test_statistical_action_qualification_cannot_fall_back_to_another_domain(
    migrated_settings: Settings, field: str
) -> None:
    payload = qualified_beta_payload(migrated_settings)
    payload["candidate_allocation"]["beta"]["policy"][field] = "synthetic-foreign-domain"
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    report = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 0
    assert "BETA_QUALIFICATION_UNAVAILABLE" in report.result.candidate_allocation.reasons
