"""Standard results through the agreed frozen case and authenticated report seam."""

from typing import Any

import pytest
from test_research_risk_veto import (
    _seed_selection_event,
    _selection_anchor_case,
    research_command,
)
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    ResultAccessScope,
    load_frozen_decision_case,
)


def outcome_case(
    settings: Settings, *, cutoff: str = "2043-02-01T16:00:00+00:00"
) -> dict[str, Any]:
    command = research_command()
    scope = ResultAccessScope(
        contract_version="1.0.0",
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        visibility="USER",
    )
    anchor, selection = _selection_anchor_case(settings, command, scope)
    _seed_selection_event(settings, anchor, anchor.decision_event_id, selection)
    payload = load_frozen_decision_case(settings).model_dump(mode="json")
    for key in (
        "case_contract_version",
        "host_contract_version",
        "report_projection_contract_version",
    ):
        payload["version_bundle"][key] = "standard-outcomes.1.0.0"
    payload["version_bundle"]["agent_definition_version"] = "2.0.0"
    payload["agent_definition"]["version"] = "2.0.0"
    payload["case_id"] = "synthetic-standard-outcomes-v1"
    payload["business_identity"] = "synthetic-standard-outcomes-v1"
    payload["knowledge_cutoff"] = cutoff
    payload["report_generated_at"] = cutoff
    payload["access_scope"] = scope.model_dump(mode="json")
    payload["standard_outcomes"] = {
        "contract_version": "1.0.0",
        "synthetic": True,
        "generator_version": "fictional-standard-outcomes/1",
        "seed": 1919,
        "selection_event_id": anchor.decision_event_id,
        "cutoff_at": cutoff,
        "market_calendar_version": "synthetic-market-calendar-v1",
        "standard_quantity": "100",
        "observations": [],
        "previous_event_id": None,
    }
    from stock_profiler.modules.evaluation.contracts import StandardOutcomeCommand

    payload["standard_outcomes"] = StandardOutcomeCommand.model_validate(
        payload["standard_outcomes"]
    ).model_dump(mode="json")
    payload["input"]["standard_outcomes"] = payload["standard_outcomes"]
    return payload


def test_due_missing_outcomes_preserve_all_frozen_selection_members(
    migrated_settings: Settings,
) -> None:
    payload = outcome_case(migrated_settings)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert execution.report is not None
    outcome = execution.report.model_dump(mode="json")["result"]["standard_outcomes"]
    assert outcome["populations"]["SELECTION"] == {
        "registered": 10,
        "due": 10,
        "evaluable": 0,
        "missing": 10,
        "immature": 0,
        "achieved": 0,
        "not_achieved": 0,
        "formal_adjudication": "INDETERMINATE",
    }
    assert len({member["evaluation_id"] for member in outcome["members"]}) == 10
    assert all(member["state"] == "UNAVAILABLE" for member in outcome["members"])


def standard_observation(*, terminal_price: str = "8") -> dict[str, Any]:
    return {
        "security_id": "synthetic-security-00",
        "entry": {
            "evidence_id": "fictional-entry-00",
            "authority": "EXCHANGE",
            "source_version": "fictional-market/1",
            "effective_at": "2042-07-02T15:00:00Z",
            "published_at": "2042-07-02T15:01:00Z",
            "acquired_at": "2042-07-02T15:02:00Z",
            "validated_at": "2042-07-02T15:03:00Z",
            "corrects_evidence_id": None,
            "correction_reason": None,
            "sessions": [
                {
                    "closed_at": "2042-07-01T15:00:00Z",
                    "buyable": False,
                    "unavailable_reason": "SUSPENDED",
                    "turnover": "0",
                    "volume": "0",
                    "costs": None,
                },
                {
                    "closed_at": "2042-07-02T15:00:00Z",
                    "buyable": True,
                    "unavailable_reason": None,
                    "turnover": "10000",
                    "volume": "1000",
                    "costs": {"commission": "5", "fees": "2", "taxes": "0", "slippage": "3"},
                },
            ],
        },
        "terminal": {
            "evidence_id": "fictional-terminal-00",
            "authority": "EXCHANGE",
            "source_version": "fictional-actions/1",
            "effective_at": "2043-01-02T15:00:00Z",
            "published_at": "2043-01-02T15:01:00Z",
            "acquired_at": "2043-01-02T15:02:00Z",
            "validated_at": "2043-01-02T15:03:00Z",
            "corrects_evidence_id": None,
            "correction_reason": None,
            "price": terminal_price,
            "quantity_multiplier": "1.5",
            "cash_distributions": "32",
            "actions_complete_through": "2043-01-02T15:00:00Z",
            "costs": {"commission": "5", "fees": "3", "taxes": "8", "slippage": "4"},
        },
    }


def bind_outcome_input(payload: dict[str, Any]) -> None:
    from stock_profiler.modules.evaluation.contracts import StandardOutcomeCommand

    payload["standard_outcomes"] = StandardOutcomeCommand.model_validate(
        payload["standard_outcomes"]
    ).model_dump(mode="json")
    payload["input"]["standard_outcomes"] = payload["standard_outcomes"]


def test_first_buyable_vwap_and_all_costs_actions_determine_terminal_success(
    migrated_settings: Settings,
) -> None:
    payload = outcome_case(migrated_settings)
    payload["standard_outcomes"]["observations"] = [standard_observation()]
    bind_outcome_input(payload)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert execution.report is not None
    outcome = execution.report.model_dump(mode="json")["result"]["standard_outcomes"]
    member = outcome["members"][0]
    assert member["entry_at"] == "2042-07-02T15:00:00Z"
    assert member["entry_price"] == "10.1"
    assert member["matures_at"] == "2043-01-02T15:00:00Z"
    assert member["net_total_return"] == "0.2"
    assert member["state"] == "ACHIEVED"
    assert outcome["populations"]["SELECTION"]["evaluable"] == 1
    assert outcome["populations"]["SELECTION"]["missing"] == 9


def successor_case(payload: dict[str, Any], event_id: str, cutoff: str) -> dict[str, Any]:
    from copy import deepcopy

    successor = deepcopy(payload)
    successor["case_id"] += ":" + cutoff
    successor["business_identity"] += ":" + cutoff
    successor["knowledge_cutoff"] = cutoff
    successor["report_generated_at"] = cutoff
    successor["standard_outcomes"]["cutoff_at"] = cutoff
    successor["standard_outcomes"]["previous_event_id"] = event_id
    return successor


def test_late_outcome_and_authoritative_correction_append_linked_report_versions(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.modules.delivery.access import AccessPrincipal

    payload = outcome_case(migrated_settings)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert first.report is not None
    late = successor_case(payload, first.report.event_id, "2043-02-02T16:00:00Z")
    late_observation = standard_observation()
    for evidence in (late_observation["entry"], late_observation["terminal"]):
        evidence["acquired_at"] = "2043-02-02T15:02:00Z"
        evidence["validated_at"] = "2043-02-02T15:03:00Z"
    late["standard_outcomes"]["observations"] = [late_observation]
    bind_outcome_input(late)
    second = run_frozen_decision_case(
        migrated_settings, late, clock=GovernanceClock(late["knowledge_cutoff"])
    )
    assert second.report is not None
    correction = successor_case(late, second.report.event_id, "2043-02-03T16:00:00Z")
    terminal = correction["standard_outcomes"]["observations"][0]["terminal"]
    terminal.update(
        {
            "evidence_id": "fictional-terminal-00-corrected",
            "price": "7.9",
            "corrects_evidence_id": "fictional-terminal-00",
            "correction_reason": "Authoritative synthetic terminal price correction.",
            "published_at": "2043-02-03T15:01:00Z",
            "acquired_at": "2043-02-03T15:02:00Z",
            "validated_at": "2043-02-03T15:03:00Z",
        }
    )
    bind_outcome_input(correction)
    third = run_frozen_decision_case(
        migrated_settings, correction, clock=GovernanceClock(correction["knowledge_cutoff"])
    )
    assert third.report is not None
    assert third.report.result.standard_outcomes is not None
    assert second.report.result.standard_outcomes is not None
    assert (
        third.report.result.standard_outcomes.previous_report_id == second.report.report_version_id
    )
    assert third.report.result.standard_outcomes.report_version == 3
    assert third.report.result.standard_outcomes.members[0].state == "NOT_ACHIEVED"
    assert second.report.result.standard_outcomes.members[0].state == "ACHIEVED"
    assert first.report.result.standard_outcomes is not None
    assert first.report.result.standard_outcomes.members[0].state == "UNAVAILABLE"
    delivery = ResultDelivery.from_settings(migrated_settings)
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    assert delivery.read_report(first.report.report_version_id, principal) == first.report
    assert delivery.read_report(second.report.report_version_id, principal) == second.report
    assert (
        run_frozen_decision_case(
            migrated_settings, correction, clock=GovernanceClock(correction["knowledge_cutoff"])
        ).report
        == third.report
    )


def seed_candidate_snapshot(settings: Settings) -> str:
    """Load independent original D0 fixture facts behind the persistence adapter."""
    import json
    from copy import deepcopy
    from pathlib import Path

    from synthetic_candidate_workspace import failed_candidate_case

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.candidate_selection.calibrated_candidates import (
        CandidateReleaseOutcome,
    )
    from stock_profiler.modules.decision_cases.domain import (
        ExternalResult,
        FrozenDecisionCase,
        StageResult,
    )

    release = json.loads(
        (Path(__file__).parents[1] / "fixtures/synthetic/candidate_workspace.json").read_text()
    )["workspace"]["releases"][0]["release"]
    payload: dict[str, Any] = dict(failed_candidate_case(settings))
    payload["knowledge_cutoff"] = "2042-06-30T23:59:59Z"
    payload["report_generated_at"] = "2042-07-01T01:00:00Z"
    payload["candidate_release"]["knowledge_cutoff"] = payload["knowledge_cutoff"]
    payload["candidate_release"]["published_at"] = payload["report_generated_at"]
    payload["candidate_release"]["market_calendar_version"] = "synthetic-market-calendar-v1"
    payload["candidate_release"]["market_sessions"] = [
        {
            "market_date": f"2042-07-{day:02}",
            "opens_at": f"2042-07-{day:02}T08:00:00Z",
            "closes_at": f"2042-07-{day:02}T15:00:00Z",
            "session_sequence": index + 1,
        }
        for index, day in enumerate((1, 2, 3, 4, 7))
    ]
    from stock_profiler.modules.candidate_selection.calibrated_candidates import (
        CandidateReleaseCommand,
    )

    payload["candidate_release"] = CandidateReleaseCommand.model_validate(
        payload["candidate_release"]
    ).model_dump(mode="json")
    payload["input"]["candidate_release"] = payload["candidate_release"]
    source = FrozenDecisionCase.model_validate(payload)
    release["knowledge_cutoff"] = payload["knowledge_cutoff"]
    release["published_at"] = payload["report_generated_at"]
    release["market_calendar_version"] = "synthetic-market-calendar-v1"
    template = release["members"][0]
    release["members"] = []
    for index in range(10):
        member = deepcopy(template)
        member.update(
            {
                "security_id": f"synthetic-security-{index:02}",
                "research_id": f"synthetic-evaluation-research-{index:02}",
                "calibrated_probability": "0.85" if index != 1 else "0.25",
                "candidate": index == 0,
                "risk_status": "ACCEPTED" if index != 2 else "REJECTED",
            }
        )
        release["members"].append(member)
    outcome = CandidateReleaseOutcome.model_validate(release)
    ledger = DecisionLedger.from_settings(
        settings, clock=GovernanceClock(payload["report_generated_at"])
    )
    ledger.persist_business_mapping_before_framework(source)
    with ledger.serialize_case_execution() as connection:
        fact = ledger.commit_event(
            connection,
            case=source,
            framework_run_id=source.framework_run_id,
            result=ExternalResult(
                outcome_code="CANDIDATE_RELEASE_CANDIDATES",
                summary="Original synthetic candidate snapshot.",
                key_reasons=("SYNTHETIC",),
                candidate_release=outcome,
            ),
            stage_results=(),
        )
        ledger.record_stage_result(
            connection,
            case=source,
            stage_result=StageResult(
                phase="BUSINESS_COMMIT", status="SUCCEEDED", gate_results=(), reasons=()
            ),
            decision_event_id=fact.decision_event_id,
        )
    with ledger.serialize_case_execution() as connection:
        ledger.publish_report(connection, fact)
        ledger.record_stage_result(
            connection,
            case=source,
            stage_result=StageResult(
                phase="PUBLICATION", status="SUCCEEDED", gate_results=(), reasons=()
            ),
            decision_event_id=fact.decision_event_id,
        )
    return fact.decision_event_id


def test_probability_and_candidate_populations_are_independent_of_user_choice(
    migrated_settings: Settings,
) -> None:
    payload = outcome_case(migrated_settings)
    seed_candidate_snapshot(migrated_settings)
    payload["standard_outcomes"]["observations"] = [standard_observation()]
    bind_outcome_input(payload)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert execution.report is not None and execution.report.result.standard_outcomes is not None
    outcome = execution.report.result.standard_outcomes
    assert outcome.populations["SELECTION"].registered == 10
    assert outcome.populations["PROBABILITY"].registered == 10
    assert outcome.populations["CANDIDATE"].registered == 1
    assert len({member.evaluation_id for member in outcome.members}) == 21
    assert {
        member.security_id for member in outcome.members if member.population == "PROBABILITY"
    } == {f"synthetic-security-{i:02}" for i in range(10)}
    assert outcome.populations["CANDIDATE"].achieved == 1
    assert outcome.populations["CANDIDATE"].formal_adjudication == "EVIDENCE_COMPLETE"
    assert outcome.populations["PROBABILITY"].formal_adjudication == "INDETERMINATE"
    assert outcome.delivery[0].expired
    assert outcome.delivery[0].disposition == "CANDIDATES"


@pytest.mark.parametrize(
    "cutoff,expected",
    [
        ("2042-07-02T16:00:00Z", "PENDING"),
        ("2043-02-01T16:00:00Z", "NOT_ACHIEVED"),
    ],
)
def test_only_mature_terminal_result_counts(
    migrated_settings: Settings, cutoff: str, expected: str
) -> None:
    payload = outcome_case(migrated_settings, cutoff=cutoff)
    observation = standard_observation(terminal_price="7.9")
    if expected == "PENDING":
        observation["terminal"] = None
    payload["standard_outcomes"]["observations"] = [observation]
    bind_outcome_input(payload)
    result = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock(cutoff))
    assert result.report is not None and result.report.result.standard_outcomes is not None
    assert result.report.result.standard_outcomes.members[0].state == expected
    assert result.report.result.standard_outcomes.populations["SELECTION"].immature == (
        10 if expected == "PENDING" else 0
    )


def test_entry_expiry_keeps_member_and_uniform_maturity(migrated_settings: Settings) -> None:
    payload = outcome_case(migrated_settings)
    observation = standard_observation()
    observation["terminal"] = None
    observation["entry"]["effective_at"] = "2042-07-07T15:00:00Z"
    for name in ("published_at", "acquired_at", "validated_at"):
        observation["entry"][name] = "2042-07-07T16:00:00Z"
    observation["entry"]["sessions"] = [
        {
            "closed_at": f"2042-07-{day:02}T15:00:00Z",
            "buyable": False,
            "unavailable_reason": "LIMIT_UP",
            "turnover": "0",
            "volume": "0",
            "costs": None,
        }
        for day in (1, 2, 3, 4, 7)
    ]
    payload["standard_outcomes"]["observations"] = [observation]
    bind_outcome_input(payload)
    result = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert result.report is not None and result.report.result.standard_outcomes is not None
    member = result.report.result.standard_outcomes.members[0]
    assert member.entry_expired and member.state == "NOT_ACHIEVED"
    assert member.matures_at.isoformat() == "2043-01-07T15:00:00+00:00"
    assert result.report.result.standard_outcomes.populations["SELECTION"].registered == 10


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("skip-first", "STANDARD_FIRST_BUYABLE_SESSION_UNPROVEN"),
        ("future", "STANDARD_EVIDENCE_NOT_AVAILABLE_AT_CUTOFF"),
        ("wrong-terminal", "STANDARD_TERMINAL_HORIZON_INCOMPLETE"),
        ("foreign-member", "STANDARD_OBSERVATION_MEMBERSHIP_INVALID"),
        ("branch", "STANDARD_REPORT_LINEAGE_CONFLICT"),
    ],
)
def test_incomplete_or_unavailable_authority_cannot_form_standard_report(
    migrated_settings: Settings, mutation: str, error: str
) -> None:
    payload = outcome_case(migrated_settings)
    observation = standard_observation()
    if mutation == "skip-first":
        observation["entry"]["sessions"].pop(0)
    elif mutation == "future":
        observation["terminal"]["validated_at"] = "2043-02-02T16:00:00Z"
    elif mutation == "wrong-terminal":
        observation["terminal"]["effective_at"] = "2043-01-01T15:00:00Z"
    elif mutation == "foreign-member":
        observation["security_id"] = "fictional-foreign-security"
    else:
        payload["standard_outcomes"]["previous_event_id"] = "fictional-nonexistent-parent"
    payload["standard_outcomes"]["observations"] = [observation]
    bind_outcome_input(payload)
    with pytest.raises(ValueError, match=error):
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
        )


def test_partial_entry_evidence_is_retained_in_immature_report(migrated_settings: Settings) -> None:
    payload = outcome_case(migrated_settings, cutoff="2042-07-01T16:00:00Z")
    observation = standard_observation()
    observation["terminal"] = None
    observation["entry"]["sessions"] = observation["entry"]["sessions"][:1]
    observation["entry"]["effective_at"] = "2042-07-01T15:00:00Z"
    for clock in ("published_at", "acquired_at", "validated_at"):
        observation["entry"][clock] = "2042-07-01T15:30:00Z"
    payload["standard_outcomes"]["observations"] = [observation]
    bind_outcome_input(payload)
    result = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert result.report is not None and result.report.result.standard_outcomes is not None
    assert result.report.result.standard_outcomes.members[0].observation is not None


def test_failed_month_has_delivery_report_without_requiring_selection_members(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_workspace import failed_candidate_case

    payload = outcome_case(migrated_settings, cutoff="2047-02-01T16:00:00Z")
    failed_payload: dict[str, Any] = dict(failed_candidate_case(migrated_settings))
    failure = run_frozen_decision_case(
        migrated_settings,
        failed_payload,
        clock=GovernanceClock(failed_payload["report_generated_at"]),
    )
    assert failure.report is not None
    payload["standard_outcomes"]["selection_event_id"] = None
    payload["standard_outcomes"]["candidate_event_id"] = failure.report.event_id
    bind_outcome_input(payload)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert report.report is not None and report.report.result.standard_outcomes is not None
    outcome = report.report.result.standard_outcomes
    assert outcome.members == ()
    assert len(outcome.delivery) == 1
    assert outcome.delivery[0].disposition in {"FAILED", "BLOCKED"}
    assert outcome.delivery[0].published_at is not None


def test_saved_delivery_snapshots_keep_reminders_correction_and_old_population(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.modules.decision_cases.service import correct_default_frozen_decision_case
    from stock_profiler.modules.delivery.access import AccessPrincipal
    from stock_profiler.modules.delivery.user_facts import UserFactRequest

    payload = outcome_case(migrated_settings)
    event_id = seed_candidate_snapshot(migrated_settings)
    ledger = DecisionLedger.from_settings(
        migrated_settings, clock=GovernanceClock("2042-07-03T16:00:00Z")
    )
    with ledger.serialize_case_execution() as connection:
        source = ledger.get_decision_event(event_id, connection)
        formal = ledger.get_formal_report_for_event(event_id, connection)
    assert source is not None and formal is not None
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    delivery = ResultDelivery.from_settings(
        migrated_settings, clock=GovernanceClock("2042-07-02T16:00:00Z")
    )
    from stock_profiler.modules.decision_cases.domain import StageResult
    from stock_profiler.modules.delivery.candidate_reminder_contracts import CandidateReminderRecord

    reminder = CandidateReminderRecord.model_validate(
        {
            "intent_id": "fictional-reminder-intent",
            "attempt_id": "fictional-reminder-attempt",
            "report_version_id": formal.report_version_id,
            "plan_month": "2042-07",
            "kind": "INITIAL",
            "request_digest": "a" * 64,
            "recorded_at": "2042-07-02T16:00:00Z",
            "body": {
                "result_type": "CANDIDATES",
                "candidate_count": 1,
                "window_ends_at": "2042-07-07T15:00:00Z",
                "entry_path": "/candidates",
            },
            "channels": [
                {"role": "PRIMARY", "result": "UNKNOWN"},
                {"role": "PERSISTENT", "result": "ACCEPTED"},
            ],
            "status": "ATTEMPTED",
            "path_completed": True,
        }
    )
    # Original fixture reminder facts are installed behind the ledger adapter.
    with ledger.serialize_case_execution() as connection:
        ledger.record_stage_result(
            connection,
            case=source.case,
            decision_event_id=event_id,
            stage_result=StageResult(
                phase="NOTIFICATION",
                status="SUCCEEDED",
                gate_results=(),
                reasons=(),
                candidate_reminder=reminder,
            ),
            recorded_at=reminder.recorded_at.isoformat(),
        )
    corrected = correct_default_frozen_decision_case(
        source.case, ledger, source.case.business_identity
    )
    assert corrected.report is not None
    result = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert result.report is not None and result.report.result.standard_outcomes is not None
    outcome = result.report.result.standard_outcomes
    original_batch, correction_batch = outcome.delivery
    assert original_batch.reminders == (reminder,)
    assert original_batch.superseded_by_event_id == corrected.report.event_id
    assert correction_batch.corrects_event_id == event_id
    assert correction_batch.report_version_id == corrected.report.report_version_id
    assert outcome.populations["PROBABILITY"].registered == 10
    assert outcome.populations["CANDIDATE"].registered == 1
    principal = principal.model_copy(update={"permissions": ("REPORT_READ", "USER_FACT")})
    assert (
        delivery.record_user_fact(
            result.report.report_version_id,
            principal,
            UserFactRequest(
                kind="CONFIRMED", choice="DECLINE", idempotency_key="fictional-personal-decline"
            ),
        )
        is not None
    )
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
        ).report
        == result.report
    )
    assert delivery.read_report(result.report.report_version_id, None) is None
    foreign = principal.model_copy(update={"account_ids": ("fictional-foreign-account",)})
    assert delivery.read_report(result.report.report_version_id, foreign) is None
    assert delivery.read_report(result.report.report_version_id, principal) == result.report


def test_threshold_uses_exact_wealth_without_rounding_up(migrated_settings: Settings) -> None:
    payload = outcome_case(migrated_settings)
    payload["standard_outcomes"]["observations"] = [
        standard_observation(terminal_price="7." + "9" * 150)
    ]
    bind_outcome_input(payload)
    result = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert result.report is not None and result.report.result.standard_outcomes is not None
    assert result.report.result.standard_outcomes.members[0].state == "NOT_ACHIEVED"


@pytest.mark.parametrize(
    "mutation,error",
    [
        ("identity", "STANDARD_EVIDENCE_IDENTITY_REDEFINED"),
        ("uncited", "STANDARD_AUTHORITATIVE_CORRECTION_REQUIRED"),
        ("quantity", "STANDARD_REPORT_CONTRACT_CHANGED"),
        ("calendar", "STANDARD_REPORT_CONTRACT_CHANGED"),
    ],
)
def test_retained_evidence_and_contract_cannot_be_rewritten(
    migrated_settings: Settings,
    mutation: str,
    error: str,
) -> None:
    payload = outcome_case(migrated_settings)
    payload["standard_outcomes"]["observations"] = [standard_observation()]
    bind_outcome_input(payload)
    first = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert first.report is not None
    successor = successor_case(payload, first.report.event_id, "2043-02-02T16:00:00Z")
    if mutation in {"identity", "uncited"}:
        terminal = successor["standard_outcomes"]["observations"][0]["terminal"]
        terminal["price"] = "7.9"
        if mutation == "uncited":
            terminal["evidence_id"] = "fictional-uncited-terminal"
    else:
        successor["standard_outcomes"][
            "standard_quantity" if mutation == "quantity" else "market_calendar_version"
        ] = "200" if mutation == "quantity" else "synthetic-market-calendar-v2"
    bind_outcome_input(successor)
    with pytest.raises(ValueError, match=error):
        run_frozen_decision_case(
            migrated_settings, successor, clock=GovernanceClock(successor["knowledge_cutoff"])
        )


def test_membership_identities_are_saved_when_source_events_are_committed(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger

    payload = outcome_case(migrated_settings)
    candidate_id = seed_candidate_snapshot(migrated_settings)
    ledger = DecisionLedger.from_settings(migrated_settings)
    with ledger.serialize_case_execution() as connection:
        selection = ledger.get_decision_event(
            payload["standard_outcomes"]["selection_event_id"], connection
        )
        candidate = ledger.get_decision_event(candidate_id, connection)
    assert selection is not None and candidate is not None
    assert len(selection.result.evaluation_registrations) == 10
    assert len(candidate.result.evaluation_registrations) == 11
    assert {item.population for item in candidate.result.evaluation_registrations} == {
        "PROBABILITY",
        "CANDIDATE",
    }
    result = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert result.report is not None and result.report.result.standard_outcomes is not None
    assert {
        item.evaluation_id
        for item in (
            *selection.result.evaluation_registrations,
            *candidate.result.evaluation_registrations,
        )
    } == {member.evaluation_id for member in result.report.result.standard_outcomes.members}


def test_candidate_route_cannot_restart_existing_evaluation_identity_history(
    migrated_settings: Settings,
) -> None:
    payload = outcome_case(migrated_settings)
    candidate_id = seed_candidate_snapshot(migrated_settings)
    payload["standard_outcomes"]["observations"] = [standard_observation()]
    bind_outcome_input(payload)
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(payload["knowledge_cutoff"])
    )
    assert original.report is not None
    attack = successor_case(payload, original.report.event_id, "2043-02-02T16:00:00Z")
    attack["standard_outcomes"].update(
        selection_event_id=None, candidate_event_id=candidate_id, previous_event_id=None
    )
    attack["standard_outcomes"]["observations"][0]["terminal"]["price"] = "7.9"
    bind_outcome_input(attack)
    with pytest.raises(ValueError, match="STANDARD_SELECTION_SOURCE_REQUIRED"):
        run_frozen_decision_case(
            migrated_settings, attack, clock=GovernanceClock(attack["knowledge_cutoff"])
        )
