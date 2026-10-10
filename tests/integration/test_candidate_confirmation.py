"""Complete confirmation journeys through the frozen case/report seam."""

from copy import deepcopy
from typing import Any, cast

import pytest
from synthetic_candidate_allocation import feasible_payload
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings


def confirmation_payload(
    settings: Settings,
    *,
    held_topup: bool = False,
    disposal_friction: str = "0",
    alternate_account_route: bool = False,
    minimum_commission: str = "0",
) -> dict[str, Any]:
    payload, _ = feasible_payload(
        settings,
        candidate_count=1 if held_topup else 2,
        security_id="XQZ-4017" if held_topup else "SYNTH-CANDIDATE",
        cutoff_at="2042-05-19T16:00:00Z",
        qualification_valid_through="2042-05-23T15:00:00Z",
    )
    for route in payload["candidate_allocation"]["routes"]:
        route["disposal_friction_ratio"] = disposal_friction
        route["minimum_commission"] = minimum_commission
    if alternate_account_route:
        routes = payload["candidate_allocation"]["routes"]
        for route in list(routes):
            alternative = deepcopy(route)
            alternative["account_id"] = "synthetic-account-8029"
            routes.append(alternative)
    payload["input"]["candidate_allocation"] = deepcopy(payload["candidate_allocation"])
    if held_topup:
        command = payload["candidate_allocation"]
        command["securities"][0]["issuer_id"] = "FICTIONAL-ORBITAL-MOSAIC"
        command["policy"].update(entry_target_ratio="0.25", neighborhood_ratio="0.29")
        payload["input"]["candidate_allocation"] = deepcopy(command)
    report = run_frozen_decision_case(
        settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_allocation is not None
    plan = report.result.candidate_allocation
    allocation = payload.pop("candidate_allocation")
    payload["input"].pop("candidate_allocation")
    payload["business_identity"] = "synthetic-confirmation:first"
    version = "candidate-confirmation.1.0.0"
    payload["version_bundle"].update(
        case_contract_version=version,
        host_contract_version=version,
        report_projection_contract_version=version,
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
            {"security_id": row.candidate.security_id, "choice": "ACCEPT"} for row in plan.rows
        ],
        "revalidation": allocation,
    }
    payload["input"]["candidate_confirmation"] = deepcopy(payload["candidate_confirmation"])
    return payload


def test_complete_confirmation_and_all_accept_reservations_replay_atomically(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    execution = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    assert execution.report is not None
    confirmation = execution.report.result.candidate_confirmation
    assert confirmation is not None and confirmation.disposition == "CONFIRMED"
    assert len(confirmation.choices) == 2
    assert len(confirmation.reservations) == 2
    assert [reservation.principal for reservation in confirmation.reservations] == [700, 700]
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
        ).report
        == execution.report
    )


def test_another_device_cannot_overwrite_the_committed_choice_vector(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert original is not None
    other = deepcopy(payload)
    other["business_identity"] = "synthetic-confirmation:other-device"
    other["candidate_confirmation"]["idempotency_key"] = "other-device"
    other["candidate_confirmation"]["choices"][1]["choice"] = "DEFER"
    other["input"]["candidate_confirmation"] = deepcopy(other["candidate_confirmation"])
    conflict = run_frozen_decision_case(
        migrated_settings, other, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert conflict is not None and conflict.result.candidate_confirmation is not None
    assert conflict.result.candidate_confirmation.reasons == ("CONFIRMATION_VERSION_CONFLICT",)
    assert conflict.result.candidate_confirmation.reservations == ()
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
        ).report
        == original
    )


@pytest.mark.parametrize("field", ["user_id", "portfolio_id", "candidate_batch_id"])
def test_submission_cannot_rebind_the_saved_plan_identity(
    migrated_settings: Settings,
    field: str,
) -> None:
    payload = confirmation_payload(migrated_settings)
    payload["candidate_confirmation"][field] = "synthetic-wrong-identity"
    payload["input"]["candidate_confirmation"] = deepcopy(payload["candidate_confirmation"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "BLOCKED"
    assert report.result.candidate_confirmation.reservations == ()


def test_saved_reservations_reduce_later_allocation_without_caller_copying_them(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    accepted = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert accepted is not None
    later = deepcopy(payload)
    command = later.pop("candidate_confirmation")
    later["input"].pop("candidate_confirmation")
    later["candidate_allocation"] = command["revalidation"]
    later["input"]["candidate_allocation"] = deepcopy(command["revalidation"])
    later["business_identity"] = "synthetic-later-allocation"
    version = "candidate-allocation.1.0.0"
    later["version_bundle"].update(
        case_contract_version=version,
        host_contract_version=version,
        report_projection_contract_version=version,
    )
    report = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.total_principal == 0
    assert all(row.candidate.candidate for row in report.result.candidate_allocation.rows)


@pytest.mark.parametrize("observed_at", ["2042-05-17T16:01:00Z", "2042-05-24T10:01:00Z"])
def test_confirmation_cannot_act_outside_the_original_window(
    migrated_settings: Settings,
    observed_at: str,
) -> None:
    payload = confirmation_payload(migrated_settings)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock(observed_at)
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "BLOCKED"
    assert report.result.candidate_confirmation.reservations == ()


def test_uncertain_submission_blocks_new_keys_until_original_identity_is_reconciled(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.modules.decision_cases.ports import DecisionEventCommitUncertainError

    payload = confirmation_payload(migrated_settings)

    def fail_commit(self: DecisionLedger, connection: object, **kwargs: object) -> None:
        raise DecisionEventCommitUncertainError("synthetic uncertain confirmation storage")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", fail_commit)
        uncertain = run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
        )
    assert uncertain.business_commit_status == "UNKNOWN" and uncertain.report is None
    other = deepcopy(payload)
    other["business_identity"] = "synthetic-confirmation:unsafe-retry"
    other["candidate_confirmation"]["idempotency_key"] = "unsafe-retry"
    other["input"]["candidate_confirmation"] = deepcopy(other["candidate_confirmation"])
    blocked = run_frozen_decision_case(
        migrated_settings, other, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert blocked is not None and blocked.result.candidate_confirmation is not None
    assert blocked.result.candidate_confirmation.reasons == ("CONFIRMATION_PENDING_RECONCILIATION",)
    assert blocked.result.candidate_confirmation.reservations == ()
    recovered = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    assert recovered.framework_run_id == uncertain.framework_run_id
    assert (
        recovered.report is not None and recovered.report.result.candidate_confirmation is not None
    )
    assert len(recovered.report.result.candidate_confirmation.reservations) == 2


def test_original_idempotency_key_converges_even_if_transport_identity_changes(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None
    replay = deepcopy(payload)
    replay["business_identity"] = "synthetic-second-transport-attempt"
    assert (
        run_frozen_decision_case(
            migrated_settings, replay, clock=GovernanceClock("2042-05-19T16:01:00Z")
        ).report
        == report
    )


def revision_payload(
    payload: dict[str, Any], confirmation_id: str, *, choice: str
) -> dict[str, Any]:
    revised = deepcopy(payload)
    command = revised["candidate_confirmation"]
    command["idempotency_key"] = "revision"
    command["seen_confirmation_id"] = confirmation_id
    command["choices"][1]["choice"] = choice
    revised["input"]["candidate_confirmation"] = deepcopy(command)
    return revised


def test_revision_cannot_release_without_post_confirmation_order_exclusion(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert original is not None
    revised = revision_payload(payload, original.event_id, choice="DEFER")
    report = run_frozen_decision_case(
        migrated_settings, revised, clock=GovernanceClock("2042-05-19T16:02:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.reasons == ("ORDER_EXCLUSION_REQUIRED",)


@pytest.mark.parametrize("choice", ["DECLINE", "DEFER"])
def test_decline_and_defer_preserve_candidates_and_leave_all_capacity_available(
    migrated_settings: Settings,
    choice: str,
) -> None:
    payload = confirmation_payload(migrated_settings)
    for row in payload["candidate_confirmation"]["choices"]:
        row["choice"] = choice
    payload["input"]["candidate_confirmation"] = deepcopy(payload["candidate_confirmation"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "CONFIRMED"
    assert report.result.candidate_confirmation.reservations == ()
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        original_plan = ledger.get_formal_report_for_event(
            payload["candidate_confirmation"]["plan_event_id"], connection
        )
        source = ledger.get_formal_report_for_event(
            payload["candidate_confirmation"]["revalidation"]["candidate_event_id"], connection
        )
    assert original_plan is not None and original_plan.result.candidate_allocation is not None
    assert original_plan.result.candidate_allocation.total_principal == 1400
    assert source is not None and source.result.candidate_release is not None
    assert all(row.candidate for row in source.result.candidate_release.members)


def save_withdrawal_proof(
    settings: Settings,
    payload: dict[str, Any],
    *,
    open_order: bool = False,
    unknown: bool = False,
    cutoff_at: str = "2042-05-19T16:02:00Z",
    case_identity: str = "withdrawal-proof",
) -> str:
    from test_position_state_reconciliation import (
        position_case_payload,
        position_evidence,
        refresh_current_position_evidence,
    )

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage

    ledger = DecisionLedger(initialize_runtime_storage(settings).engine)
    with ledger.serialize_case_execution() as connection:
        risk = ledger.get_decision_event(
            payload["candidate_confirmation"]["revalidation"]["risk_handoff"][
                "concentration_event_id"
            ],
            connection,
        )
    assert risk is not None and risk.case.concentration is not None
    snapshot = risk.case.concentration.position_snapshot.model_dump(mode="json")
    snapshot["snapshot_id"] = "synthetic-withdrawal-snapshot"
    snapshot["cutoff_at"] = cutoff_at
    refresh_current_position_evidence(snapshot, snapshot["cutoff_at"])
    if open_order:
        account = snapshot["accounts"][0]
        cash = account["cash_state"]
        from decimal import Decimal

        for field in ("trading_cash", "transferable_cash"):
            cash[field] = str(Decimal(cash[field]) - 20)
        cash["frozen_cash"] = "20"
        account["open_orders"] = [
            {
                "order_id": "synthetic-withdrawal-open-buy",
                "security_id": "SYNTH-CANDIDATE-1",
                "side": "BUY",
                "remaining_quantity": None if unknown else "2",
                "reserved_cash": "20",
                "reserved_cash_semantics": "BROKER_FINAL_RESERVED_CASH",
                "evidence": position_evidence(
                    "synthetic-withdrawal-order", cutoff_at=snapshot["cutoff_at"]
                ),
            }
        ]
    report = run_frozen_decision_case(
        settings,
        position_case_payload(settings, case_identity, snapshot),
        clock=GovernanceClock(snapshot["cutoff_at"]),
    ).report
    assert report is not None
    return report.event_id


@pytest.mark.parametrize("proof", ["complete", "unfinished", "unknown"])
def test_revision_atomically_preserves_existing_reservations_and_gates_release(
    migrated_settings: Settings,
    proof: str,
) -> None:
    payload = confirmation_payload(migrated_settings)
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert original is not None and original.result.candidate_confirmation is not None
    before = original.result.candidate_confirmation
    revised = revision_payload(payload, original.event_id, choice="DEFER")
    revised["candidate_confirmation"]["operation"] = "WITHDRAW"
    revised["candidate_confirmation"]["withdrawal_position_event_id"] = save_withdrawal_proof(
        migrated_settings, payload, open_order=proof != "complete", unknown=proof == "unknown"
    )
    revised["input"]["candidate_confirmation"] = deepcopy(revised["candidate_confirmation"])
    report = run_frozen_decision_case(
        migrated_settings, revised, clock=GovernanceClock("2042-05-19T16:02:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    result = report.result.candidate_confirmation
    if proof == "complete":
        assert result.disposition == "CONFIRMED"
        assert result.supersedes_confirmation_id == before.confirmation_id
        assert result.reservations == before.reservations[:1]
        assert result.released_reservation_ids == (before.reservations[1].commitment_id,)
    else:
        assert result.disposition == "BLOCKED" and result.reservations == ()
        assert result.released_reservation_ids == ()
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:02:00Z")
        ).report
        == original
    )


def test_competing_devices_commit_one_complete_vector_without_partial_reservations(
    migrated_settings: Settings,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    payload = confirmation_payload(migrated_settings)
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
    outcomes = [report.result.candidate_confirmation for report in reports]
    assert sorted(outcome.disposition for outcome in outcomes) == ["BLOCKED", "CONFIRMED"]
    loser = next(outcome for outcome in outcomes if outcome.disposition == "BLOCKED")
    assert loser.reasons == ("CONFIRMATION_VERSION_CONFLICT",) and loser.reservations == ()


@pytest.mark.parametrize(
    "change",
    [
        "missing-choice",
        "duplicate-choice",
        "zero-row",
        "permission",
        "unconfirmed-cap",
        "price",
        "cash",
        "plan-version",
    ],
)
def test_full_policy_and_choice_conflicts_never_create_partial_reservations(
    migrated_settings: Settings,
    change: str,
) -> None:
    payload = confirmation_payload(migrated_settings)
    command = payload["candidate_confirmation"]
    if change == "missing-choice":
        command["choices"].pop()
    elif change == "duplicate-choice":
        command["choices"][1] = deepcopy(command["choices"][0])
    elif change == "zero-row":
        command["choices"].append({"security_id": "SYNTHETIC-ZERO-ROW", "choice": "ACCEPT"})
    elif change == "permission":
        command["revalidation"]["routes"][0]["permission"] = False
    elif change == "unconfirmed-cap":
        command["revalidation"]["routes"][0]["price_cap_confirmed"] = False
    elif change == "price":
        command["revalidation"]["routes"][0]["price_cap"] = "11"
    elif change == "cash":
        route = command["revalidation"]["routes"][0]
        command["revalidation"]["commitments"] = [
            {
                "commitment_id": "synthetic-concurrent-capacity",
                "account_id": route["account_id"],
                "security_id": route["security_id"],
                "issuer_id": command["revalidation"]["securities"][0]["issuer_id"],
                "broker_order_id": None,
                "principal": "10",
                "quantity": "1",
                "price_cap": "10",
                "purchase_cost": "0",
                "disposal_friction": "0",
                "evidence": deepcopy(route["evidence"]),
            }
        ]
    else:
        command["plan_id"] = "synthetic-wrong-plan-version"
    payload["input"]["candidate_confirmation"] = deepcopy(command)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "BLOCKED"
    assert report.result.candidate_confirmation.reservations == ()


@pytest.mark.parametrize("fault", ["before-commit", "after-commit"])
def test_confirmation_storage_failure_is_atomic_and_original_retry_recovers(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
    from stock_profiler.modules.decision_cases.ports import (
        DecisionEventCommitError,
        DecisionEventCommitUncertainError,
    )

    payload = confirmation_payload(migrated_settings)
    original_commit = DecisionLedger.commit_event

    def commit(self: DecisionLedger, *args: Any, **kwargs: Any) -> Any:
        if fault == "after-commit":
            original_commit(self, *args, **kwargs)
            raise DecisionEventCommitUncertainError("synthetic lost acknowledgement")
        raise DecisionEventCommitError("synthetic atomic rollback")

    with monkeypatch.context() as patch:
        patch.setattr(DecisionLedger, "commit_event", commit)
        attempt = run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
        )
    case = FrozenDecisionCase.model_validate(payload)
    assert case.access_scope is not None
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        history = ledger.candidate_confirmation_history(
            connection, case.access_scope, payload["candidate_confirmation"]["portfolio_id"]
        )
    if fault == "before-commit":
        assert attempt.business_commit_status == "FAILED" and attempt.report is None
        assert history == ()
    else:
        assert attempt.business_commit_status == "COMMITTED" and len(history) == 1
    recovered = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    assert (
        recovered.report is not None and recovered.report.result.candidate_confirmation is not None
    )
    assert len(recovered.report.result.candidate_confirmation.reservations) == 2
    assert recovered.framework_run_id == attempt.framework_run_id
    with ledger.serialize_case_execution() as connection:
        history = ledger.candidate_confirmation_history(
            connection, case.access_scope, payload["candidate_confirmation"]["portfolio_id"]
        )
    assert len(history) == 1


def test_draft_and_generic_delivery_actions_cannot_save_confirmation(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
    from stock_profiler.modules.decision_cases.domain import FrozenDecisionCase
    from stock_profiler.modules.delivery.access import AccessPrincipal
    from stock_profiler.modules.delivery.user_facts import UserFactRequest

    payload = confirmation_payload(migrated_settings)
    case = FrozenDecisionCase.model_validate(payload)
    command = payload["candidate_confirmation"]
    command["choices"][0]["choice"] = "DEFER"  # local editing only
    delivery = ResultDelivery.from_settings(migrated_settings)
    assert case.access_scope is not None
    principal = AccessPrincipal(
        user_id=case.access_scope.user_id,
        account_ids=case.access_scope.account_ids,
        permissions=("REPORT_READ", "USER_FACT", "CANDIDATE_ALLOCATION"),
    )
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        plan_report = ledger.get_formal_report_for_event(command["plan_event_id"], connection)
    assert plan_report is not None
    assert (
        delivery.record_user_fact(
            plan_report.report_version_id,
            principal,
            UserFactRequest(
                kind="CONFIRMED", choice="ACCEPT", idempotency_key="synthetic-single-row"
            ),
        )
        is None
    )
    assert delivery.audit_history()[-1].reason == "CANDIDATE_READ_ONLY"
    with ledger.serialize_case_execution() as connection:
        assert (
            ledger.candidate_confirmation_history(
                connection, case.access_scope, command["portfolio_id"]
            )
            == ()
        )


def test_confirmation_rechecks_window_at_actual_commit_boundary(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger

    payload = confirmation_payload(migrated_settings)
    clock = GovernanceClock("2042-05-19T16:01:00Z")
    original_record = DecisionLedger.record_stage_result

    def record(self: DecisionLedger, *args: Any, **kwargs: Any) -> Any:
        result = original_record(self, *args, **kwargs)
        if kwargs["stage_result"].phase == "CANDIDATE_CONFIRMATION":
            clock.current = datetime.fromisoformat("2042-05-24T16:01:00+00:00")
        return result

    monkeypatch.setattr(DecisionLedger, "record_stage_result", record)
    report = run_frozen_decision_case(migrated_settings, payload, clock=clock).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "BLOCKED"
    assert report.result.candidate_confirmation.reservations == ()


def test_equivalent_revalidation_with_fresh_provenance_keeps_exact_plan_structure(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    for route in payload["candidate_confirmation"]["revalidation"]["routes"]:
        route["evidence"]["source_version"] = "synthetic-refreshed-buy-observation-v2"
    payload["input"]["candidate_confirmation"] = deepcopy(payload["candidate_confirmation"])
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "CONFIRMED"
    assert [row.principal for row in report.result.candidate_confirmation.reservations] == [
        700,
        700,
    ]


def test_new_accept_appends_a_revision_without_replacing_the_original_vector(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    payload["candidate_confirmation"]["choices"][1]["choice"] = "DEFER"
    payload["input"]["candidate_confirmation"] = deepcopy(payload["candidate_confirmation"])
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert original is not None and original.result.candidate_confirmation is not None
    revised = revision_payload(payload, original.event_id, choice="ACCEPT")
    report = run_frozen_decision_case(
        migrated_settings, revised, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    result = report.result.candidate_confirmation
    assert result.disposition == "CONFIRMED" and len(result.reservations) == 2
    assert result.reservations[0] == original.result.candidate_confirmation.reservations[0]
    assert result.supersedes_confirmation_id == original.event_id
    assert result.released_reservation_ids == ()
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
        ).report
        == original
    )


@pytest.mark.parametrize("proof_cutoff", ["2042-05-19T16:02:00Z", "2042-05-24T16:02:00Z"])
def test_explicit_withdrawal_appends_release_and_keeps_original_plan_unchanged(
    migrated_settings: Settings,
    proof_cutoff: str,
) -> None:
    payload = confirmation_payload(migrated_settings)
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert original is not None and original.result.candidate_confirmation is not None
    revised = revision_payload(payload, original.event_id, choice="DECLINE")
    command = revised["candidate_confirmation"]
    command["operation"] = "WITHDRAW"
    command["choices"][0]["choice"] = "DECLINE"
    command["withdrawal_position_event_id"] = save_withdrawal_proof(
        migrated_settings, payload, cutoff_at=proof_cutoff
    )
    revised["input"]["candidate_confirmation"] = deepcopy(command)
    report = run_frozen_decision_case(
        migrated_settings, revised, clock=GovernanceClock(proof_cutoff)
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    result = report.result.candidate_confirmation
    assert result.disposition == "CONFIRMED" and result.reservations == ()
    assert result.released_reservation_ids == tuple(
        row.commitment_id for row in original.result.candidate_confirmation.reservations
    )
    assert result.supersedes_confirmation_id == original.event_id


def test_idempotency_key_cannot_be_rebound_to_different_choices(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.modules.decision_cases.ports import DecisionEventCommitError

    payload = confirmation_payload(migrated_settings)
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    revised = deepcopy(payload)
    revised["candidate_confirmation"]["choices"][0]["choice"] = "DECLINE"
    revised["input"]["candidate_confirmation"] = deepcopy(revised["candidate_confirmation"])
    with pytest.raises(DecisionEventCommitError, match="idempotency key payload conflict"):
        run_frozen_decision_case(
            migrated_settings, revised, clock=GovernanceClock("2042-05-19T16:01:00Z")
        )
    assert (
        run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
        ).report
        == original
    )


@pytest.mark.parametrize("family", ["market", "correlation"])
def test_expired_current_evidence_prevents_confirmation_even_with_old_valid_cutoff(
    migrated_settings: Settings,
    family: str,
) -> None:
    payload = confirmation_payload(migrated_settings)
    command = payload["candidate_confirmation"]
    facts = (
        command["revalidation"]["securities"]
        if family == "market"
        else [command["revalidation"]["correlations"]]
    )
    for fact in facts:
        fact["evidence"]["expires_at"] = "2042-05-19T16:00:30Z"
    payload["input"]["candidate_confirmation"] = deepcopy(command)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "BLOCKED"
    assert report.result.candidate_confirmation.reservations == ()


def test_optimization_cannot_cross_the_final_confirmation_window(
    migrated_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    from stock_profiler.modules.decision_cases import candidate_confirmation

    payload = confirmation_payload(migrated_settings)
    clock = GovernanceClock("2042-05-19T16:01:00Z")
    from stock_profiler.modules.decision_cases.candidate_allocation import (
        adjudicate_candidate_allocation,
    )

    original = adjudicate_candidate_allocation

    calls = 0

    def replay(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 1:
            clock.current = datetime.fromisoformat("2042-05-24T16:01:00+00:00")
        return result

    monkeypatch.setattr(candidate_confirmation, "adjudicate_candidate_allocation", replay)
    report = run_frozen_decision_case(migrated_settings, payload, clock=clock).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "BLOCKED"
    assert report.result.candidate_confirmation.reservations == ()


def test_a_corrected_plan_can_release_reservations_with_complete_order_exclusion(
    migrated_settings: Settings,
) -> None:
    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
    from stock_profiler.bootstrap.decision_cases import correct_default_frozen_decision_case

    payload = confirmation_payload(migrated_settings)
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert original is not None
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    with ledger.serialize_case_execution() as connection:
        plan = ledger.get_decision_event(
            payload["candidate_confirmation"]["plan_event_id"], connection
        )
    assert plan is not None
    correction = correct_default_frozen_decision_case(
        migrated_settings,
        plan.case.business_identity,
        clock=GovernanceClock("2042-05-19T16:02:00Z"),
    )
    assert correction.report is not None
    revised = revision_payload(payload, original.event_id, choice="DECLINE")
    command = revised["candidate_confirmation"]
    command["operation"] = "WITHDRAW"
    command["choices"][0]["choice"] = "DECLINE"
    command["withdrawal_position_event_id"] = save_withdrawal_proof(migrated_settings, payload)
    revised["input"]["candidate_confirmation"] = deepcopy(command)
    report = run_frozen_decision_case(
        migrated_settings, revised, clock=GovernanceClock("2042-05-19T16:02:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "CONFIRMED"
    assert len(report.result.candidate_confirmation.released_reservation_ids) == 2


def test_new_position_facts_block_confirmation_using_older_risk_handoffs(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    save_withdrawal_proof(migrated_settings, payload, open_order=True)
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:02:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "BLOCKED"
    assert report.result.candidate_confirmation.reservations == ()


def test_narrow_scope_cannot_receive_reservation_details_from_a_wider_portfolio(
    migrated_settings: Settings,
) -> None:
    from synthetic_candidate_workspace import failed_candidate_case

    from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
    from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
    from stock_profiler.modules.decision_cases.domain import (
        ExternalResult,
        FrozenDecisionCase,
        StageResult,
    )

    payload = confirmation_payload(migrated_settings)
    accepted = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert accepted is not None
    ledger = DecisionLedger(initialize_runtime_storage(migrated_settings).engine)
    narrow_source = cast(dict[str, Any], failed_candidate_case(migrated_settings))
    narrow_source["access_scope"]["account_ids"] = ["synthetic-account-8029"]
    narrow_source["input"]["account"]["account_id"] = "synthetic-account-8029"
    source_case = FrozenDecisionCase.model_validate(narrow_source)
    ledger.persist_business_mapping_before_framework(source_case)
    with ledger.serialize_case_execution() as connection:
        source = ledger.get_formal_report_for_event(
            payload["candidate_confirmation"]["revalidation"]["candidate_event_id"], connection
        )
        assert source is not None
        fact = ledger.commit_event(
            connection,
            case=source_case,
            framework_run_id=source_case.framework_run_id,
            result=ExternalResult(
                outcome_code="CANDIDATES",
                summary="Synthetic narrowed candidate scope.",
                key_reasons=(),
                candidate_release=source.result.candidate_release,
            ),
            stage_results=(),
        )
        ledger.record_stage_result(
            connection,
            case=source_case,
            framework_run_id=source_case.framework_run_id,
            decision_event_id=fact.decision_event_id,
            stage_result=StageResult(
                phase="BUSINESS_COMMIT", status="SUCCEEDED", gate_results=(), reasons=()
            ),
        )
        ledger.publish_report(connection, fact)
        ledger.record_stage_result(
            connection,
            case=source_case,
            framework_run_id=source_case.framework_run_id,
            decision_event_id=fact.decision_event_id,
            stage_result=StageResult(
                phase="PUBLICATION", status="SUCCEEDED", gate_results=(), reasons=()
            ),
        )
        assert ledger.get_formal_report_for_event(fact.decision_event_id, connection) is not None
    later = deepcopy(payload)
    command = later.pop("candidate_confirmation")
    later["input"].pop("candidate_confirmation")
    allocation = command["revalidation"]
    allocation["candidate_event_id"] = fact.decision_event_id
    later["candidate_allocation"] = allocation
    later["input"]["candidate_allocation"] = deepcopy(allocation)
    later["business_identity"] = "synthetic-narrowed-reservation-probe"
    later["access_scope"]["account_ids"] = ["synthetic-account-8029"]
    later["input"]["account"]["account_id"] = "synthetic-account-8029"
    version = "candidate-allocation.1.0.0"
    later["version_bundle"].update(
        case_contract_version=version,
        host_contract_version=version,
        report_projection_contract_version=version,
    )
    report = run_frozen_decision_case(
        migrated_settings, later, clock=GovernanceClock("2042-05-19T16:02:00Z")
    ).report
    assert report is not None and report.result.candidate_allocation is not None
    assert report.result.candidate_allocation.disposition == "BLOCKED"
    assert report.result.candidate_allocation.commitments == ()


def test_shadow_attempt_cannot_claim_the_user_confirmation_idempotency_key(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings)
    shadow = deepcopy(payload)
    shadow["access_scope"]["visibility"] = "SHADOW"
    result = run_frozen_decision_case(
        migrated_settings, shadow, clock=GovernanceClock("2042-05-19T16:01:00Z")
    )
    assert result.report is None
    report = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "CONFIRMED"


def test_withdrawal_does_not_confuse_unchanged_prior_holdings_with_new_execution(
    migrated_settings: Settings,
) -> None:
    payload = confirmation_payload(migrated_settings, held_topup=True)
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert original is not None and original.result.candidate_confirmation is not None
    assert original.result.candidate_confirmation.reservations
    revised = deepcopy(payload)
    command = revised["candidate_confirmation"]
    command.update(
        operation="WITHDRAW",
        idempotency_key="synthetic-held-withdraw",
        seen_confirmation_id=original.event_id,
        withdrawal_position_event_id=save_withdrawal_proof(migrated_settings, payload),
    )
    command["choices"][0]["choice"] = "DECLINE"
    revised["input"]["candidate_confirmation"] = deepcopy(command)
    report = run_frozen_decision_case(
        migrated_settings, revised, clock=GovernanceClock("2042-05-19T16:02:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "CONFIRMED"
    assert report.result.candidate_confirmation.reservations == ()


def test_reservation_stress_friction_is_independent_of_ambient_decimal_context(
    migrated_settings: Settings,
) -> None:
    from decimal import Decimal, Inexact, localcontext

    payload = confirmation_payload(migrated_settings, disposal_friction="0.0137")
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        report = run_frozen_decision_case(
            migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
        ).report
    assert report is not None and report.result.candidate_confirmation is not None
    assert report.result.candidate_confirmation.disposition == "CONFIRMED"
    assert all(
        row.disposal_friction == Decimal("9.59")
        for row in report.result.candidate_confirmation.reservations
    )


@pytest.mark.parametrize("operation", ["WITHDRAW", "SUBMIT"])
@pytest.mark.parametrize("unknown", [False, True])
def test_release_cannot_use_an_older_clean_proof_after_new_order_facts(
    migrated_settings: Settings, operation: str, unknown: bool
) -> None:
    payload = confirmation_payload(migrated_settings)
    original = run_frozen_decision_case(
        migrated_settings, payload, clock=GovernanceClock("2042-05-19T16:01:00Z")
    ).report
    assert original is not None
    clean_proof = save_withdrawal_proof(migrated_settings, payload)
    new_proof = save_withdrawal_proof(
        migrated_settings,
        payload,
        open_order=True,
        unknown=unknown,
        case_identity="later-order-proof",
    )
    assert new_proof != clean_proof
    revised = revision_payload(payload, original.event_id, choice="DECLINE")
    command = revised["candidate_confirmation"]
    command["operation"] = operation
    command["withdrawal_position_event_id"] = clean_proof
    revised["input"]["candidate_confirmation"] = deepcopy(command)
    report = run_frozen_decision_case(
        migrated_settings, revised, clock=GovernanceClock("2042-05-19T16:02:00Z")
    ).report
    assert report is not None and report.result.candidate_confirmation is not None
    result = report.result.candidate_confirmation
    assert result.disposition == "BLOCKED"
    assert result.released_reservation_ids == ()
    assert result.reservations == ()
