from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from test_scoped_qualification import GovernanceClock

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases import service as decision_case_service
from stock_profiler.modules.decision_cases.domain import (
    FrozenDecisionCase,
    load_frozen_decision_case,
)
from stock_profiler.modules.delivery.access import AccessPrincipal
from stock_profiler.modules.portfolio.contracts import PortfolioUseCommand
from stock_profiler.modules.portfolio.service import adjudicate as adjudicate_portfolio


def portfolio_proposal() -> dict[str, Any]:
    fixture = portfolio_fixture()
    assert fixture["synthetic"] is True
    assert fixture["generator_version"] == "portfolio-authorization-fixture-v1"
    assert fixture["seed"] == 6101
    return deepcopy(cast(dict[str, Any], fixture["proposal"]))


def portfolio_case_payload(
    settings: Settings,
    identity: str,
    command: dict[str, Any],
    *,
    account_ids: list[str] | None = None,
) -> dict[str, Any]:
    payload = load_frozen_decision_case(settings).model_dump(mode="json")
    proposal = command.get("proposal", {})
    snapshot = proposal.get("snapshot", {}) if isinstance(proposal, dict) else {}
    activation_snapshot = (
        proposal.get("activation_snapshot", {}) if isinstance(proposal, dict) else {}
    )
    evidence_snapshot = (
        activation_snapshot
        if isinstance(activation_snapshot, dict) and activation_snapshot
        else snapshot
    )
    visible_accounts = (
        [account["account_id"] for account in snapshot.get("accounts", [])]
        if isinstance(snapshot, dict) and snapshot
        else account_ids or []
    )
    payload["business_identity"] = f"synthetic:portfolio:{identity}"
    payload["case_id"] = f"d0-portfolio-{identity}"
    payload["version_bundle"].update(
        case_contract_version="6.0.0",
        host_contract_version="6.0.0",
        report_projection_contract_version="6.0.0",
        agent_definition_version="2.0.0",
    )
    payload["agent_definition"]["version"] = "2.0.0"
    payload["access_scope"] = {
        "contract_version": "1.0.0",
        "user_id": "stock-profiler-single-user",
        "account_ids": visible_accounts,
        "visibility": "USER",
    }
    if isinstance(evidence_snapshot, dict) and evidence_snapshot:
        payload["knowledge_cutoff"] = evidence_snapshot["cutoff_at"]
    payload["portfolio"] = command
    return payload


def portfolio_confirmation_command(
    proposal: dict[str, Any],
    *,
    previous_authorization_id: str | None = None,
    confirmed_at: str | None = None,
) -> dict[str, Any]:
    confirmation_suffix = proposal["risk_budget"]["version_id"].rsplit("-", maxsplit=1)[-1]
    return {
        "operation": "PORTFOLIO_CONFIRM",
        "proposal": proposal,
        "previous_authorization_id": previous_authorization_id,
        "confirmation": {
            "confirmation_id": f"synthetic-risk-confirmation-{confirmation_suffix}",
            "user_id": "stock-profiler-single-user",
            "portfolio_id": proposal["portfolio_id"],
            "snapshot_id": proposal["snapshot"]["snapshot_id"],
            "risk_budget_version_id": proposal["risk_budget"]["version_id"],
            "confirmed_at": confirmed_at or proposal["snapshot"]["cutoff_at"],
            "confirmed": True,
        },
    }


def snapshot_at(
    snapshot: dict[str, Any],
    *,
    snapshot_id: str,
    cutoff: str,
) -> dict[str, Any]:
    version = deepcopy(snapshot)
    version["snapshot_id"] = snapshot_id
    version["cutoff_at"] = cutoff
    for account in version["accounts"]:
        account["captured_at"] = cutoff
    return version


def next_portfolio_proposal() -> dict[str, Any]:
    proposal = portfolio_proposal()
    proposal["cash_obligations"] = []
    selection_cutoff = "2042-05-18T16:00:00Z"
    activation_cutoff = "2042-05-19T16:00:00Z"
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-portfolio-snapshot-beta",
        cutoff=selection_cutoff,
    )
    next_account = deepcopy(cast(dict[str, Any], portfolio_fixture()["next_cash_account"]))
    next_account["captured_at"] = selection_cutoff
    proposal["snapshot"]["accounts"].append(next_account)
    proposal["snapshot"]["selected_account_ids"].append("synthetic-account-8029")
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-portfolio-snapshot-beta-activation",
        cutoff=activation_cutoff,
    )
    proposal["risk_budget"].update(
        version_id="synthetic-risk-budget-beta",
        effective_at=activation_cutoff,
        expires_at="2042-11-19T16:00:00Z",
    )
    return proposal


def reduced_portfolio_proposal() -> dict[str, Any]:
    proposal = next_portfolio_proposal()
    selection_cutoff = "2042-05-20T16:00:00Z"
    activation_cutoff = "2042-05-21T16:00:00Z"
    account = deepcopy(proposal["snapshot"]["accounts"][-1])
    account.update(
        captured_at=selection_cutoff,
        cash_fact_id="synthetic-cash-fact-8029-funded-gamma",
    )
    proposal["snapshot"] = {
        "snapshot_id": "synthetic-portfolio-snapshot-gamma",
        "cutoff_at": selection_cutoff,
        "accounts": [account],
        "selected_account_ids": ["synthetic-account-8029"],
    }
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-portfolio-snapshot-gamma-activation",
        cutoff=activation_cutoff,
    )
    proposal["risk_budget"].update(
        version_id="synthetic-risk-budget-gamma",
        effective_at=activation_cutoff,
        expires_at="2042-11-21T16:00:00Z",
    )
    obligation = deepcopy(portfolio_proposal()["cash_obligations"][0])
    obligation["obligation_id"] = "synthetic-cash-obligation-gamma"
    obligation["amount"] = "95.25"
    obligation["target_account_id"] = "synthetic-account-8029"
    proposal["cash_obligations"] = [obligation]
    return proposal


def unobligated_portfolio_proposal() -> dict[str, Any]:
    proposal = portfolio_proposal()
    proposal["cash_obligations"] = []
    return proposal


def relaxed_portfolio_proposal() -> dict[str, Any]:
    proposal = unobligated_portfolio_proposal()
    selection_cutoff = "2042-06-16T15:00:00Z"
    activation_cutoff = "2042-06-17T16:00:00Z"
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-portfolio-snapshot-relaxed",
        cutoff=selection_cutoff,
    )
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-portfolio-snapshot-relaxed-activation",
        cutoff=activation_cutoff,
    )
    proposal["risk_budget"].update(
        version_id="synthetic-risk-budget-relaxed",
        effective_at=activation_cutoff,
        expires_at="2042-12-17T16:00:00Z",
    )
    proposal["risk_budget"]["concentration"]["hard_ratio"] = "0.20"
    return proposal


def scheduled_portfolio_proposal() -> dict[str, Any]:
    proposal = unobligated_portfolio_proposal()
    selection_cutoff = "2042-05-17T16:02:00Z"
    activation_cutoff = "2042-05-18T16:00:00Z"
    proposal["snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-portfolio-snapshot-scheduled",
        cutoff=selection_cutoff,
    )
    proposal["activation_snapshot"] = snapshot_at(
        proposal["snapshot"],
        snapshot_id="synthetic-portfolio-snapshot-scheduled-activation",
        cutoff=activation_cutoff,
    )
    proposal["risk_budget"].update(
        version_id="synthetic-risk-budget-scheduled",
        effective_at=activation_cutoff,
        expires_at="2042-11-18T16:00:00Z",
    )
    return proposal


def relaxation_evidence(previous_authorization_id: str) -> dict[str, Any]:
    evidence = deepcopy(cast(dict[str, Any], portfolio_fixture()["risk_relaxation_evidence"]))
    evidence["predecessor_authorization_id"] = previous_authorization_id
    return evidence


def downside_grid_requalification(
    previous_authorization_id: str,
    *,
    action_policy_version_id: str,
) -> dict[str, Any]:
    return {
        "evidence_id": f"synthetic-grid-requalification-{action_policy_version_id}",
        "predecessor_authorization_id": previous_authorization_id,
        "predecessor_risk_budget_version_id": "synthetic-risk-budget-alpha",
        "action_policy_version_id": action_policy_version_id,
        "historical_out_of_sample_evidence_id": "synthetic-grid-history-grid-beta",
        "historical_completed_at": "2042-06-10T15:00:00Z",
        "locked_forward_confirmation_id": "synthetic-grid-forward-grid-beta",
        "locked_forward_confirmed_at": "2042-06-16T14:59:00Z",
        "available_at": "2042-06-16T15:00:00Z",
    }


def proposal_account_ids(proposal: dict[str, Any]) -> list[str]:
    return [account["account_id"] for account in proposal["snapshot"]["accounts"]]


def proposal_activation_cutoff(proposal: dict[str, Any]) -> str:
    activation = proposal.get("activation_snapshot")
    if isinstance(activation, dict):
        return cast(str, activation["cutoff_at"])
    return cast(str, proposal["snapshot"]["cutoff_at"])


def result_family(name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (
                Path(__file__).parents[1]
                / "fixtures"
                / "synthetic"
                / "result-families"
                / f"{name}.json"
            ).read_text(encoding="utf-8")
        ),
    )


def portfolio_fixture() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (
                Path(__file__).parents[1]
                / "fixtures"
                / "synthetic"
                / "portfolio_authorization.json"
            ).read_text(encoding="utf-8")
        ),
    )


def test_preview_shows_full_cash_scope_and_excluded_account_types(
    migrated_settings: Settings,
) -> None:
    command = {
        "operation": "PORTFOLIO_PREVIEW",
        "proposal": portfolio_proposal(),
    }

    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(migrated_settings, "preview", deepcopy(command)),
        clock=GovernanceClock(),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "PREVIEWED"
    assert outcome.authorization is None
    preview = outcome.preview
    assert preview is not None
    assert preview.included_account_ids == ("synthetic-account-4017",)
    assert preview.included_accounts[0].scope == "FULL_ACCOUNT"
    assert preview.included_accounts[0].account_type == "SIMULATED_CASH"
    assert preview.included_accounts[0].cash_fact_id == "synthetic-cash-fact-4017"
    assert preview.excluded_accounts[0].account_id == "synthetic-account-margin-2001"
    assert preview.excluded_accounts[0].reason == "UNSUPPORTED_ACCOUNT_TYPE"


def test_preview_exposes_currency_permissions_and_fact_freshness(
    migrated_settings: Settings,
) -> None:
    proposal = portfolio_proposal()
    proposal["snapshot"]["accounts"][0].update(
        currency="XSP",
        permissions=["SIMULATED_STATISTICAL_ACTION"],
    )
    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "account-metadata",
            {"operation": "PORTFOLIO_PREVIEW", "proposal": proposal},
        ),
        clock=GovernanceClock(),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None and outcome.preview is not None
    account = outcome.preview.included_accounts[0].model_dump(mode="json")
    assert account["currency"] == "XSP"
    assert account["permissions"] == ["SIMULATED_STATISTICAL_ACTION"]
    assert account["captured_at"] == "2042-05-17T16:00:00Z"


def test_dated_cash_obligation_is_not_rejected_for_its_calendar_position(
    migrated_settings: Settings,
) -> None:
    proposal = portfolio_proposal()
    proposal["cash_obligations"][0]["latest_usable_at"] = "2043-01-18T16:00:00Z"
    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "obligation-outside-budget",
            portfolio_confirmation_command(proposal),
        ),
        clock=GovernanceClock(),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "APPROVED"
    assert outcome.authorization is not None
    assert outcome.authorization.proposal.cash_obligations[0].latest_usable_at.isoformat() == (
        "2043-01-18T16:00:00+00:00"
    )


def test_portfolio_evidence_after_the_frozen_cutoff_cannot_authorize(
    migrated_settings: Settings,
) -> None:
    proposal = portfolio_proposal()
    delayed_cutoff = "2042-05-17T16:00:01Z"
    proposal["snapshot"]["cutoff_at"] = delayed_cutoff
    for account in proposal["snapshot"]["accounts"]:
        account["captured_at"] = delayed_cutoff
    proposal["risk_budget"].update(
        effective_at=delayed_cutoff,
        expires_at="2042-11-17T16:00:01Z",
    )
    payload = portfolio_case_payload(
        migrated_settings,
        "evidence-after-cutoff",
        portfolio_confirmation_command(proposal),
    )
    payload["knowledge_cutoff"] = "2042-05-17T16:00:00Z"

    execution = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock("2042-05-18T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("PORTFOLIO_EVIDENCE_AFTER_CUTOFF",)
    assert outcome.authorization is None


def test_invalid_portfolio_cutoff_is_rejected_before_creating_a_run(
    migrated_settings: Settings,
) -> None:
    payload = portfolio_case_payload(
        migrated_settings,
        "invalid-cutoff",
        {"operation": "PORTFOLIO_PREVIEW", "proposal": portfolio_proposal()},
    )
    payload["knowledge_cutoff"] = "not-a-timestamp"

    with pytest.raises(ValueError):
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())


def test_inherited_portfolio_evidence_after_the_cutoff_is_not_available(
    migrated_settings: Settings,
) -> None:
    late_proposal = next_portfolio_proposal()
    late = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "late-history-source",
            portfolio_confirmation_command(late_proposal),
        ),
        clock=GovernanceClock("2042-05-19T16:01:00Z"),
    )
    assert late.report is not None
    payload = portfolio_case_payload(
        migrated_settings,
        "late-history-target",
        {
            "operation": "PORTFOLIO_USE",
            "portfolio_id": late_proposal["portfolio_id"],
            "authorization_id": late.decision_event_id,
            "requested_action": "NEW_EXPOSURE",
        },
        account_ids=proposal_account_ids(late_proposal),
    )
    payload["knowledge_cutoff"] = "2042-05-17T16:00:00Z"

    execution = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock("2042-05-19T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("PORTFOLIO_AUTHORIZATION_REQUIRED",)
    assert outcome.preview is None
    assert outcome.usage is None


def test_non_success_business_result_blocks_new_exposure_but_retains_protection(
    migrated_settings: Settings,
) -> None:
    proposal = portfolio_proposal()
    authorization = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "business-use-source",
            portfolio_confirmation_command(proposal),
        ),
        clock=GovernanceClock(),
    )
    assert authorization.report is not None
    source = result_family("result-failed")

    def use(identity: str, requested_action: str) -> Any:
        payload = portfolio_case_payload(
            migrated_settings,
            identity,
            {
                "operation": "PORTFOLIO_USE",
                "portfolio_id": proposal["portfolio_id"],
                "authorization_id": authorization.decision_event_id,
                "requested_action": requested_action,
            },
            account_ids=proposal_account_ids(proposal),
        )
        payload["input"] = source["input"]
        payload["expected_external_result"] = source["expected_external_result"]
        return run_frozen_decision_case(
            migrated_settings,
            payload,
            clock=GovernanceClock("2042-05-18T16:01:00Z"),
        )

    blocked = use("business-use-blocked", "NEW_EXPOSURE")
    assert blocked.report is not None
    blocked_outcome = blocked.report.result.portfolio
    assert blocked_outcome is not None
    assert blocked_outcome.disposition == "DENIED"
    assert blocked_outcome.reasons == ("BUSINESS_PREREQUISITE_NOT_MET",)
    assert blocked_outcome.preview is None
    assert blocked_outcome.usage is None

    protected = use("business-use-protected", "DETERMINISTIC_PROTECTION")
    assert protected.report is not None
    protected_outcome = protected.report.result.portfolio
    assert protected_outcome is not None
    assert protected_outcome.disposition == "APPROVED"
    assert protected_outcome.reasons == ("DETERMINISTIC_PROTECTION_RETAINED",)


def test_deterministic_protection_survives_a_conflicted_authorization_history(
    migrated_settings: Settings,
) -> None:
    proposal = portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "conflicted-history-original",
            portfolio_confirmation_command(proposal),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    original_outcome = original.report.result.portfolio
    assert original_outcome is not None
    authorization = original_outcome.authorization
    assert authorization is not None
    conflicting_outcome = original_outcome.model_copy(
        update={
            "authorization": authorization.model_copy(
                update={
                    "authorization_id": "synthetic-conflicting-portfolio-authorization",
                }
            )
        }
    )
    history = (original_outcome, conflicting_outcome)

    def use(requested_action: str) -> Any:
        return adjudicate_portfolio(
            PortfolioUseCommand.model_validate(
                {
                    "operation": "PORTFOLIO_USE",
                    "portfolio_id": proposal["portfolio_id"],
                    "authorization_id": original.decision_event_id,
                    "requested_action": requested_action,
                }
            ),
            event_id=f"synthetic-conflicted-history-{requested_action.lower()}",
            observed_at="2042-05-18T16:01:00Z",
            knowledge_cutoff="2042-05-18T16:01:00Z",
            history=history,
            lineage_history=history,
            access_account_ids=tuple(proposal_account_ids(proposal)),
        )

    assert use("NEW_EXPOSURE").reasons == ("AUTHORIZATION_HISTORY_CONFLICT",)
    protected = use("DETERMINISTIC_PROTECTION")
    assert protected.disposition == "APPROVED"
    assert protected.reasons == ("DETERMINISTIC_PROTECTION_RETAINED",)


def test_forward_version_retains_unfinished_obligations_from_the_predecessor(
    migrated_settings: Settings,
) -> None:
    original_proposal = portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "retained-obligation-original",
            portfolio_confirmation_command(original_proposal),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    successor_proposal = next_portfolio_proposal()
    successor_proposal["cash_obligations"] = []
    successor = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "retained-obligation-successor",
            portfolio_confirmation_command(
                successor_proposal,
                previous_authorization_id=original.decision_event_id,
            ),
        ),
        clock=GovernanceClock("2042-05-19T16:01:00Z"),
    )
    assert successor.report is not None
    usage_payload = portfolio_case_payload(
        migrated_settings,
        "retained-obligation-use",
        {
            "operation": "PORTFOLIO_USE",
            "portfolio_id": successor_proposal["portfolio_id"],
            "authorization_id": successor.decision_event_id,
            "requested_action": "NEW_EXPOSURE",
        },
        account_ids=proposal_account_ids(successor_proposal),
    )
    usage_payload["knowledge_cutoff"] = proposal_activation_cutoff(successor_proposal)
    usage = run_frozen_decision_case(
        migrated_settings,
        usage_payload,
        clock=GovernanceClock("2042-05-19T16:02:00Z"),
    )

    assert usage.report is not None
    outcome = usage.report.result.portfolio
    assert outcome is not None and outcome.usage is not None
    assert outcome.usage.unfinished_cash_obligations[0].obligation_id == (
        "synthetic-cash-obligation-alpha"
    )


def test_risk_budget_relaxation_requires_frozen_activation_evidence(
    migrated_settings: Settings,
) -> None:
    original_proposal = unobligated_portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "relaxation-original",
            portfolio_confirmation_command(original_proposal),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
        confirmed_at="2042-06-16T15:00:00Z",
    )
    payload = portfolio_case_payload(migrated_settings, "relaxation-missing-evidence", command)
    payload["knowledge_cutoff"] = "2042-06-17T16:00:00Z"

    execution = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock("2042-06-17T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("RISK_BUDGET_RELAXATION_EVIDENCE_REQUIRED",)


def test_risk_budget_relaxation_requires_twenty_frozen_normal_market_sessions(
    migrated_settings: Settings,
) -> None:
    original_proposal = unobligated_portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "relaxation-short-normal-window-original",
            portfolio_confirmation_command(original_proposal),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
        confirmed_at="2042-06-16T15:00:00Z",
    )
    evidence = relaxation_evidence(original.decision_event_id)
    evidence["normal_market_sessions"] = evidence["normal_market_sessions"][:19]
    command["confirmation"]["relaxation_evidence"] = evidence
    payload = portfolio_case_payload(
        migrated_settings,
        "relaxation-short-normal-window",
        command,
    )
    payload["knowledge_cutoff"] = "2042-06-17T16:00:00Z"

    with pytest.raises(ValueError, match="at least 20"):
        run_frozen_decision_case(
            migrated_settings,
            payload,
            clock=GovernanceClock("2042-06-17T16:01:00Z"),
        )


def test_risk_budget_relaxation_requires_consecutive_calendar_sessions(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "relaxation-consecutive-original",
            portfolio_confirmation_command(unobligated_portfolio_proposal()),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
    )
    evidence = relaxation_evidence(original.decision_event_id)
    evidence["normal_market_sessions"][10]["market_session_ordinal"] = 6121
    command["confirmation"]["relaxation_evidence"] = evidence

    with pytest.raises(ValueError, match="consecutive"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "relaxation-nonconsecutive-window",
                command,
            ),
            clock=GovernanceClock("2042-06-17T16:01:00Z"),
        )


def test_risk_budget_relaxation_requires_one_immutable_market_calendar_version(
    migrated_settings: Settings,
) -> None:
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id="synthetic-predecessor-authorization",
    )
    evidence = relaxation_evidence("synthetic-predecessor-authorization")
    evidence["normal_market_sessions"][10]["market_calendar_version_id"] = (
        "synthetic-market-calendar-v2"
    )
    command["confirmation"]["relaxation_evidence"] = evidence

    with pytest.raises(ValueError, match="immutable market calendar version"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "relaxation-multiple-calendars",
                command,
            ),
            clock=GovernanceClock("2042-06-17T16:01:00Z"),
        )


def test_risk_budget_relaxation_requires_a_known_market_calendar_version(
    migrated_settings: Settings,
) -> None:
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id="synthetic-predecessor-authorization",
    )
    evidence = relaxation_evidence("synthetic-predecessor-authorization")
    for session in evidence["normal_market_sessions"]:
        session["market_calendar_version_id"] = "synthetic-market-calendar-v2"
    command["confirmation"]["relaxation_evidence"] = evidence

    with pytest.raises(ValueError, match="known immutable market calendar"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "relaxation-unknown-calendar",
                command,
            ),
            clock=GovernanceClock("2042-06-17T16:01:00Z"),
        )


def test_risk_budget_relaxation_rejects_ordinals_outside_the_calendar(
    migrated_settings: Settings,
) -> None:
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id="synthetic-predecessor-authorization",
    )
    evidence = relaxation_evidence("synthetic-predecessor-authorization")
    for session in evidence["normal_market_sessions"]:
        session["market_session_ordinal"] += 100
    command["confirmation"]["relaxation_evidence"] = evidence

    with pytest.raises(ValueError, match="immutable market calendar"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "relaxation-unmapped-market-ordinal",
                command,
            ),
            clock=GovernanceClock("2042-06-17T16:01:00Z"),
        )


def test_risk_budget_relaxation_rejects_a_market_close_outside_the_calendar(
    migrated_settings: Settings,
) -> None:
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id="synthetic-predecessor-authorization",
    )
    evidence = relaxation_evidence("synthetic-predecessor-authorization")
    evidence["normal_market_sessions"][1]["closed_at"] = "2042-05-20T15:00:01Z"
    command["confirmation"]["relaxation_evidence"] = evidence

    with pytest.raises(ValueError, match="immutable market calendar"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "relaxation-unmapped-market-close",
                command,
            ),
            clock=GovernanceClock("2042-06-17T16:01:00Z"),
        )


def test_risk_budget_relaxation_requires_the_next_calendar_monthly_cutoff(
    migrated_settings: Settings,
) -> None:
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id="synthetic-predecessor-authorization",
    )
    evidence = relaxation_evidence("synthetic-predecessor-authorization")
    evidence["monthly_selection_cutoff_at"] = "2042-06-18T16:00:00Z"
    command["confirmation"]["relaxation_evidence"] = evidence

    with pytest.raises(ValueError, match="next monthly selection cutoff"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "relaxation-wrong-monthly-cutoff",
                command,
            ),
            clock=GovernanceClock("2042-06-17T16:01:00Z"),
        )


def test_risk_budget_relaxation_requires_reconfirmation_after_normal_evidence(
    migrated_settings: Settings,
) -> None:
    original_proposal = unobligated_portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "relaxation-reconfirmation-original",
            portfolio_confirmation_command(original_proposal),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    proposal["snapshot"]["cutoff_at"] = "2042-06-15T14:00:00Z"
    for account in proposal["snapshot"]["accounts"]:
        account["captured_at"] = "2042-06-15T14:00:00Z"
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
        confirmed_at="2042-06-15T15:00:00Z",
    )
    command["confirmation"]["relaxation_evidence"] = relaxation_evidence(original.decision_event_id)
    payload = portfolio_case_payload(
        migrated_settings,
        "relaxation-reconfirmation",
        command,
    )
    payload["knowledge_cutoff"] = "2042-06-17T16:00:00Z"

    execution = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock("2042-06-17T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("RISK_BUDGET_RELAXATION_EVIDENCE_INVALID",)


def test_risk_budget_relaxation_requires_normal_evidence_through_reconfirmation(
    migrated_settings: Settings,
) -> None:
    original_proposal = unobligated_portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "relaxation-stale-normal-evidence-original",
            portfolio_confirmation_command(original_proposal),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
        confirmed_at="2042-06-17T15:00:00Z",
    )
    command["confirmation"]["relaxation_evidence"] = relaxation_evidence(original.decision_event_id)
    payload = portfolio_case_payload(
        migrated_settings,
        "relaxation-stale-normal-evidence",
        command,
    )
    payload["knowledge_cutoff"] = "2042-06-17T16:00:00Z"

    execution = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock("2042-06-17T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("RISK_BUDGET_RELAXATION_EVIDENCE_INVALID",)


def test_risk_budget_relaxation_allows_reconfirmation_after_the_final_normal_close(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "relaxation-post-close-original",
            portfolio_confirmation_command(unobligated_portfolio_proposal()),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
        confirmed_at="2042-06-16T15:00:01Z",
    )
    command["confirmation"]["relaxation_evidence"] = relaxation_evidence(original.decision_event_id)

    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "relaxation-post-close-confirmation",
            command,
        ),
        clock=GovernanceClock("2042-06-17T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "APPROVED"
    assert outcome.authorization is not None


def test_risk_budget_relaxation_requires_normal_unobligated_monthly_handoff(
    migrated_settings: Settings,
) -> None:
    original_proposal = unobligated_portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "relaxation-handoff-original",
            portfolio_confirmation_command(original_proposal),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
        confirmed_at="2042-06-16T15:00:00Z",
    )
    command["confirmation"]["relaxation_evidence"] = relaxation_evidence(original.decision_event_id)
    payload = portfolio_case_payload(migrated_settings, "relaxation-handoff", command)
    payload["knowledge_cutoff"] = "2042-06-17T16:00:00Z"

    execution = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock("2042-06-17T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "APPROVED"
    assert outcome.authorization is not None
    evidence = outcome.authorization.confirmation.relaxation_evidence
    assert evidence is not None
    assert evidence.normal_from_at.isoformat() > "2042-05-17T16:00:00+00:00"
    assert len(evidence.normal_market_sessions) == 20
    assert outcome.authorization.proposal.activation_snapshot is not None
    assert (
        outcome.authorization.proposal.activation_snapshot.cutoff_at
        == evidence.monthly_selection_cutoff_at
    )


def test_risk_budget_relaxation_cannot_drop_unfinished_obligations(
    migrated_settings: Settings,
) -> None:
    original_proposal = portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "relaxation-obligation-original",
            portfolio_confirmation_command(original_proposal),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
        confirmed_at="2042-06-16T15:00:00Z",
    )
    command["confirmation"]["relaxation_evidence"] = relaxation_evidence(original.decision_event_id)
    payload = portfolio_case_payload(migrated_settings, "relaxation-obligation", command)
    payload["knowledge_cutoff"] = "2042-06-17T16:00:00Z"

    execution = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock("2042-06-17T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("RISK_BUDGET_RELAXATION_OBLIGATIONS_UNRESOLVED",)


def test_downside_grid_change_requires_action_policy_requalification(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "grid-requalification-original",
            portfolio_confirmation_command(unobligated_portfolio_proposal()),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    proposal["risk_budget"].update(
        action_policy_version_id="synthetic-action-policy-grid-beta",
        downside_grid=["0.40", "0.90"],
    )
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
        confirmed_at="2042-06-16T15:00:00Z",
    )
    command["confirmation"]["relaxation_evidence"] = relaxation_evidence(original.decision_event_id)

    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "grid-requalification-missing",
            command,
        ),
        clock=GovernanceClock("2042-06-17T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("DOWNSIDE_GRID_REQUALIFICATION_REQUIRED",)


def test_downside_grid_change_requires_a_new_action_policy_version(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "grid-requalification-version-original",
            portfolio_confirmation_command(unobligated_portfolio_proposal()),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    proposal["risk_budget"].update(
        action_policy_version_id="synthetic-action-policy-alpha",
        downside_grid=["0.40", "0.90"],
    )
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
        confirmed_at="2042-06-16T15:00:00Z",
    )
    command["confirmation"]["relaxation_evidence"] = relaxation_evidence(original.decision_event_id)
    command["confirmation"]["downside_grid_requalification"] = downside_grid_requalification(
        original.decision_event_id,
        action_policy_version_id="synthetic-action-policy-alpha",
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "grid-requalification-version-reused",
            command,
        ),
        clock=GovernanceClock("2042-06-17T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("DOWNSIDE_GRID_ACTION_POLICY_VERSION_REQUIRED",)


def test_downside_grid_change_freezes_new_action_policy_requalification(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "grid-requalification-approved-original",
            portfolio_confirmation_command(unobligated_portfolio_proposal()),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = relaxed_portfolio_proposal()
    action_policy_version_id = "synthetic-action-policy-grid-beta"
    proposal["risk_budget"].update(
        action_policy_version_id=action_policy_version_id,
        downside_grid=["0.40", "0.90"],
    )
    command = portfolio_confirmation_command(
        proposal,
        previous_authorization_id=original.decision_event_id,
        confirmed_at="2042-06-16T15:00:00Z",
    )
    command["confirmation"]["relaxation_evidence"] = relaxation_evidence(original.decision_event_id)
    command["confirmation"]["downside_grid_requalification"] = downside_grid_requalification(
        original.decision_event_id,
        action_policy_version_id=action_policy_version_id,
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "grid-requalification-approved",
            command,
        ),
        clock=GovernanceClock("2042-06-17T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "APPROVED"
    assert outcome.authorization is not None
    assert outcome.authorization.proposal.risk_budget.action_policy_version_id == (
        action_policy_version_id
    )
    requalification = outcome.authorization.confirmation.downside_grid_requalification
    assert requalification is not None
    assert requalification.action_policy_version_id == action_policy_version_id
    assert requalification.historical_out_of_sample_evidence_id == (
        "synthetic-grid-history-grid-beta"
    )
    assert requalification.locked_forward_confirmation_id == "synthetic-grid-forward-grid-beta"


def test_confirmation_freezes_the_risk_budget_and_cash_obligation_in_the_report(
    migrated_settings: Settings,
) -> None:
    proposal = portfolio_proposal()
    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "confirmation",
            portfolio_confirmation_command(deepcopy(proposal)),
        ),
        clock=GovernanceClock(),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "APPROVED"
    authorization = outcome.authorization
    assert authorization is not None
    assert authorization.authorization_id == execution.decision_event_id
    assert authorization.confirmation.confirmation_id == "synthetic-risk-confirmation-alpha"
    assert authorization.proposal.risk_budget.version_id == "synthetic-risk-budget-alpha"
    assert (
        authorization.proposal.risk_budget.effective_at.isoformat() == "2042-05-17T16:00:00+00:00"
    )
    assert authorization.proposal.risk_budget.expires_at.isoformat() == "2042-11-17T16:00:00+00:00"
    assert authorization.proposal.risk_budget.concentration.target_ratio == Decimal("0.13")
    assert authorization.proposal.risk_budget.concentration.hard_ratio == Decimal("0.19")
    assert authorization.proposal.risk_budget.stress.target_ratio == Decimal("0.14")
    assert authorization.proposal.risk_budget.stress.hard_ratio == Decimal("0.18")
    assert authorization.proposal.risk_budget.cash.target_ratio == Decimal("0.39")
    assert authorization.proposal.risk_budget.cash.hard_ratio == Decimal("0.23")
    assert authorization.proposal.risk_budget.drawdown.caution_ratio == Decimal("0.11")
    assert authorization.proposal.risk_budget.drawdown.defensive_ratio == Decimal("0.16")
    assert authorization.proposal.risk_budget.drawdown.preservation_ratio == Decimal("0.21")
    assert authorization.proposal.risk_budget.downside_grid == (Decimal("0.04"), Decimal("0.09"))
    assert authorization.proposal.risk_budget.protection_floor.retained_directions == (
        "REDUCE",
        "EXIT",
    )
    assert authorization.proposal.cash_obligations[0].amount == Decimal("125.50")
    assert authorization.proposal.cash_obligations[0].purpose == "synthetic-near-term-liquidity"
    assert (
        authorization.proposal.cash_obligations[0].latest_usable_at.isoformat()
        == "2042-08-17T16:00:00+00:00"
    )
    assert authorization.proposal.cash_obligations[0].target_account_id == "synthetic-account-4017"
    assert any(
        stage.phase == "PORTFOLIO_AUTHORIZATION"
        and stage.status == "SUCCEEDED"
        and stage.gate_results[0].gate_id == "PORTFOLIO_SCOPE"
        for stage in execution.report.stage_results
    )
    assert (
        get_formal_report(
            execution.report_version_id,
            migrated_settings,
            principal=AccessPrincipal(
                user_id="stock-profiler-single-user",
                account_ids=tuple(proposal_account_ids(proposal)),
                permissions=("REPORT_READ",),
            ),
        )
        == execution.report
    )


def test_correction_retains_the_saved_portfolio_authorization(
    migrated_settings: Settings,
) -> None:
    proposal = portfolio_proposal()
    payload = portfolio_case_payload(
        migrated_settings,
        "correction-retains-portfolio",
        portfolio_confirmation_command(proposal),
    )
    original = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock(),
    )
    assert original.report is not None
    assert original.report.result.portfolio is not None

    case = FrozenDecisionCase.model_validate(payload)
    correction = decision_case_service.correct_default_frozen_decision_case(
        case,
        DecisionLedger.from_settings(
            migrated_settings,
            clock=GovernanceClock("2042-05-18T16:01:00Z"),
        ),
        case.business_identity,
    )

    assert correction.report.result.portfolio == original.report.result.portfolio


@pytest.mark.parametrize(
    ("account_type", "expected_reason"),
    [
        ("SIMULATED_MARGIN", "UNSUPPORTED_ACCOUNT_TYPE"),
        ("SIMULATED_SECURITIES_BORROWING", "UNSUPPORTED_ACCOUNT_TYPE"),
        ("SIMULATED_PLEDGED", "UNSUPPORTED_ACCOUNT_TYPE"),
        ("SIMULATED_COLLATERAL", "UNSUPPORTED_ACCOUNT_TYPE"),
        ("SIMULATED_LIABILITY", "UNSUPPORTED_ACCOUNT_TYPE"),
        ("SIMULATED_UNCLASSIFIED", "UNKNOWN_ACCOUNT_TYPE"),
    ],
)
def test_non_cash_or_unknown_accounts_cannot_receive_statistical_authorization(
    migrated_settings: Settings, account_type: str, expected_reason: str
) -> None:
    proposal = portfolio_proposal()
    proposal["snapshot"]["accounts"][1]["account_type"] = account_type
    proposal["snapshot"]["selected_account_ids"].append("synthetic-account-margin-2001")

    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            f"blocked-{account_type.lower()}",
            portfolio_confirmation_command(deepcopy(proposal)),
        ),
        clock=GovernanceClock(),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == (expected_reason,)
    assert outcome.authorization is None
    preview = outcome.preview
    assert preview is not None
    assert preview.blocking_account_ids == ("synthetic-account-margin-2001",)
    assert preview.blocking_accounts[0].account_type == account_type
    assert preview.blocking_accounts[0].reason == expected_reason


def test_virtual_account_slices_and_non_calendar_budget_expiry_are_rejected(
    migrated_settings: Settings,
) -> None:
    virtual_slice = portfolio_proposal()
    virtual_slice["snapshot"]["accounts"][0]["scope"] = "VIRTUAL_SLICE"
    with pytest.raises(ValueError, match="FULL_ACCOUNT"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "virtual-slice",
                {"operation": "PORTFOLIO_PREVIEW", "proposal": virtual_slice},
            ),
            clock=GovernanceClock(),
        )

    invalid_expiry = portfolio_proposal()
    invalid_expiry["risk_budget"]["expires_at"] = "2042-11-16T16:00:00Z"
    with pytest.raises(ValueError, match="exactly six calendar months"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "invalid-expiry",
                {"operation": "PORTFOLIO_PREVIEW", "proposal": invalid_expiry},
            ),
            clock=GovernanceClock(),
        )


@pytest.mark.parametrize(
    ("fixture", "business_status"),
    [
        ("input-rejected", "REJECTED"),
        ("result-abstained", "ABSTAINED"),
        ("result-failed", "FAILED"),
    ],
)
def test_non_success_business_results_cannot_create_portfolio_authorization(
    migrated_settings: Settings, fixture: str, business_status: str
) -> None:
    source = result_family(fixture)
    payload = portfolio_case_payload(
        migrated_settings,
        f"business-{fixture}",
        portfolio_confirmation_command(portfolio_proposal()),
    )
    payload["input"] = source["input"]
    payload["expected_external_result"] = source["expected_external_result"]

    execution = run_frozen_decision_case(
        migrated_settings,
        payload,
        clock=GovernanceClock(),
    )

    assert execution.report is not None
    assert execution.business_result_status == business_status
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("BUSINESS_PREREQUISITE_NOT_MET",)
    assert outcome.authorization is None
    assert outcome.preview is not None
    stage = next(
        stage
        for stage in execution.report.stage_results
        if stage.phase == "PORTFOLIO_AUTHORIZATION"
    )
    assert stage.status == "REJECTED"
    assert stage.reasons == outcome.reasons


@pytest.mark.parametrize(
    ("reused_identity", "expected_reason"),
    [
        ("risk_budget", "RISK_BUDGET_VERSION_REUSE"),
        ("snapshot", "PORTFOLIO_SNAPSHOT_VERSION_REUSE"),
        ("confirmation", "PORTFOLIO_CONFIRMATION_REUSE"),
    ],
)
def test_forward_authorizations_cannot_reuse_frozen_version_or_confirmation_evidence(
    migrated_settings: Settings, reused_identity: str, expected_reason: str
) -> None:
    original_proposal = portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            f"identity-original-{reused_identity}",
            portfolio_confirmation_command(deepcopy(original_proposal)),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None

    replacement_proposal = next_portfolio_proposal()
    command = portfolio_confirmation_command(
        replacement_proposal,
        previous_authorization_id=original.decision_event_id,
    )
    if reused_identity == "risk_budget":
        replacement_proposal["risk_budget"]["version_id"] = original_proposal["risk_budget"][
            "version_id"
        ]
        replacement_proposal["risk_budget"]["concentration"]["target_ratio"] = "0.12"
        command = portfolio_confirmation_command(
            replacement_proposal,
            previous_authorization_id=original.decision_event_id,
        )
    elif reused_identity == "snapshot":
        replacement_proposal["snapshot"]["snapshot_id"] = original_proposal["snapshot"][
            "snapshot_id"
        ]
        command = portfolio_confirmation_command(
            replacement_proposal,
            previous_authorization_id=original.decision_event_id,
        )
    else:
        command["confirmation"]["confirmation_id"] = "synthetic-risk-confirmation-alpha"

    reused = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            f"identity-reused-{reused_identity}",
            command,
        ),
        clock=GovernanceClock("2042-05-19T16:01:00Z"),
    )

    assert reused.report is not None
    outcome = reused.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == (expected_reason,)
    assert outcome.authorization is None


def test_scope_or_budget_changes_require_a_new_forward_authorization_version(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "forward-original",
            portfolio_confirmation_command(portfolio_proposal()),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    original_report = original.report.model_dump_json()

    changed_proposal = next_portfolio_proposal()
    missing_predecessor = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "forward-missing-predecessor",
            portfolio_confirmation_command(deepcopy(changed_proposal)),
        ),
        clock=GovernanceClock("2042-05-19T16:01:00Z"),
    )
    assert missing_predecessor.report is not None
    missing_outcome = missing_predecessor.report.result.portfolio
    assert missing_outcome is not None
    assert missing_outcome.disposition == "DENIED"
    assert missing_outcome.reasons == ("PREVIOUS_AUTHORIZATION_REQUIRED",)

    replacement = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "forward-replacement",
            portfolio_confirmation_command(
                deepcopy(changed_proposal),
                previous_authorization_id=original.decision_event_id,
            ),
        ),
        clock=GovernanceClock("2042-05-19T16:02:00Z"),
    )
    assert replacement.report is not None
    replacement_outcome = replacement.report.result.portfolio
    assert replacement_outcome is not None
    assert replacement_outcome.disposition == "APPROVED"
    replacement_authorization = replacement_outcome.authorization
    assert replacement_authorization is not None
    assert replacement_authorization.previous_authorization_id == original.decision_event_id
    assert replacement_authorization.proposal.risk_budget.version_id == "synthetic-risk-budget-beta"
    assert replacement_authorization.proposal.snapshot.selected_account_ids == (
        "synthetic-account-4017",
        "synthetic-account-8029",
    )
    assert replacement_authorization.proposal.activation_snapshot is not None
    assert (
        replacement_authorization.proposal.activation_snapshot.snapshot_id
        == "synthetic-portfolio-snapshot-beta-activation"
    )
    assert (
        replacement_authorization.proposal.activation_snapshot.cutoff_at.isoformat()
        == "2042-05-19T16:00:00+00:00"
    )
    assert original.report.model_dump_json() == original_report
    replacement_report = replacement.report.model_dump_json()

    reduced_proposal = reduced_portfolio_proposal()
    reduced = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "forward-reduced",
            portfolio_confirmation_command(
                deepcopy(reduced_proposal),
                previous_authorization_id=replacement.decision_event_id,
            ),
        ),
        clock=GovernanceClock("2042-05-21T16:02:00Z"),
    )
    assert reduced.report is not None
    reduced_outcome = reduced.report.result.portfolio
    assert reduced_outcome is not None
    assert reduced_outcome.disposition == "APPROVED"
    reduced_authorization = reduced_outcome.authorization
    assert reduced_authorization is not None
    assert reduced_authorization.previous_authorization_id == replacement.decision_event_id
    assert reduced_authorization.proposal.risk_budget.version_id == "synthetic-risk-budget-gamma"
    assert reduced_authorization.proposal.snapshot.selected_account_ids == (
        "synthetic-account-8029",
    )
    assert reduced_authorization.proposal.snapshot.accounts[0].cash_fact_id == (
        "synthetic-cash-fact-8029-funded-gamma"
    )
    assert reduced_authorization.proposal.cash_obligations[0].amount == Decimal("95.25")
    assert reduced_authorization.proposal.cash_obligations[0].target_account_id == (
        "synthetic-account-8029"
    )
    assert replacement.report.model_dump_json() == replacement_report


def test_forward_authorization_requires_a_post_confirmation_activation_snapshot(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "forward-activation-original",
            portfolio_confirmation_command(portfolio_proposal()),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = next_portfolio_proposal()
    proposal.pop("activation_snapshot")
    proposal["risk_budget"].update(
        effective_at=proposal["snapshot"]["cutoff_at"],
        expires_at="2042-11-18T16:00:00Z",
    )

    with pytest.raises(ValueError, match="post-confirmation activation snapshot"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "forward-without-activation-snapshot",
                portfolio_confirmation_command(
                    proposal,
                    previous_authorization_id=original.decision_event_id,
                ),
            ),
            clock=GovernanceClock("2042-05-19T16:01:00Z"),
        )


def test_forward_activation_snapshot_must_follow_user_confirmation(
    migrated_settings: Settings,
) -> None:
    proposal = next_portfolio_proposal()

    with pytest.raises(ValueError, match="activation snapshot must follow user confirmation"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "forward-activation-before-confirmation",
                portfolio_confirmation_command(
                    proposal,
                    previous_authorization_id="synthetic-predecessor-authorization",
                    confirmed_at=proposal_activation_cutoff(proposal),
                ),
            ),
            clock=GovernanceClock("2042-05-19T16:01:00Z"),
        )


def test_forward_authorization_requires_distinct_activation_snapshot_identity(
    migrated_settings: Settings,
) -> None:
    proposal = next_portfolio_proposal()
    activation = proposal["activation_snapshot"]
    assert isinstance(activation, dict)
    activation["snapshot_id"] = proposal["snapshot"]["snapshot_id"]

    with pytest.raises(ValueError, match="distinct snapshot identity"):
        run_frozen_decision_case(
            migrated_settings,
            portfolio_case_payload(
                migrated_settings,
                "forward-reused-activation-snapshot",
                portfolio_confirmation_command(
                    proposal,
                    previous_authorization_id="synthetic-predecessor-authorization",
                ),
            ),
            clock=GovernanceClock("2042-05-19T16:01:00Z"),
        )


def test_post_confirmation_activation_replaces_the_prior_authorization(
    migrated_settings: Settings,
) -> None:
    original_proposal = portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "future-original",
            portfolio_confirmation_command(deepcopy(original_proposal)),
        ),
        clock=GovernanceClock("2042-05-17T16:01:00Z"),
    )
    assert original.report is not None

    replacement_proposal = scheduled_portfolio_proposal()

    def use(
        identity: str,
        authorization_id: str,
        proposal: dict[str, Any],
        observed_at: str,
    ) -> Any:
        payload = portfolio_case_payload(
            migrated_settings,
            identity,
            {
                "operation": "PORTFOLIO_USE",
                "portfolio_id": proposal["portfolio_id"],
                "authorization_id": authorization_id,
                "requested_action": "NEW_EXPOSURE",
            },
            account_ids=proposal_account_ids(proposal),
        )
        payload["knowledge_cutoff"] = observed_at
        return run_frozen_decision_case(
            migrated_settings,
            payload,
            clock=GovernanceClock(observed_at),
        )

    prior_before_effective = use(
        "future-prior-before-effective",
        original.decision_event_id,
        original_proposal,
        "2042-05-17T16:03:00Z",
    )
    assert prior_before_effective.report is not None
    prior_outcome = prior_before_effective.report.result.portfolio
    assert prior_outcome is not None
    assert prior_outcome.disposition == "APPROVED"
    assert prior_outcome.reasons == ("NEW_EXPOSURE_AUTHORIZED",)

    replacement = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "future-replacement",
            portfolio_confirmation_command(
                deepcopy(replacement_proposal),
                previous_authorization_id=original.decision_event_id,
                confirmed_at="2042-05-17T16:02:00Z",
            ),
        ),
        clock=GovernanceClock("2042-05-18T16:00:00Z"),
    )
    assert replacement.report is not None
    replacement_outcome = replacement.report.result.portfolio
    assert replacement_outcome is not None
    assert replacement_outcome.disposition == "APPROVED"
    assert replacement_outcome.authorization is not None
    assert replacement_outcome.authorization.proposal.activation_snapshot is not None
    assert (
        replacement_outcome.authorization.proposal.activation_snapshot.cutoff_at.isoformat()
        == "2042-05-18T16:00:00+00:00"
    )

    replacement_after_activation = use(
        "future-replacement-after-activation",
        replacement.decision_event_id,
        replacement_proposal,
        "2042-05-18T16:01:00Z",
    )
    assert replacement_after_activation.report is not None
    replacement_after_outcome = replacement_after_activation.report.result.portfolio
    assert replacement_after_outcome is not None
    assert replacement_after_outcome.disposition == "APPROVED"
    assert replacement_after_outcome.reasons == ("NEW_EXPOSURE_AUTHORIZED",)

    prior_after_effective = use(
        "future-prior-after-effective",
        original.decision_event_id,
        original_proposal,
        "2042-05-18T16:01:00Z",
    )
    assert prior_after_effective.report is not None
    prior_after_outcome = prior_after_effective.report.result.portfolio
    assert prior_after_outcome is not None
    assert prior_after_outcome.disposition == "DENIED"
    assert prior_after_outcome.reasons == ("CURRENT_PORTFOLIO_AUTHORIZATION_REQUIRED",)


def test_delayed_frozen_case_cannot_fork_a_later_portfolio_lineage(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "lineage-original",
            portfolio_confirmation_command(portfolio_proposal()),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    successor = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "lineage-successor",
            portfolio_confirmation_command(
                next_portfolio_proposal(),
                previous_authorization_id=original.decision_event_id,
            ),
        ),
        clock=GovernanceClock("2042-05-19T16:00:00Z"),
    )
    assert successor.report is not None
    successor_outcome = successor.report.result.portfolio
    assert successor_outcome is not None
    assert successor_outcome.disposition == "APPROVED"

    delayed = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "lineage-delayed-fork",
            portfolio_confirmation_command(
                scheduled_portfolio_proposal(),
                previous_authorization_id=original.decision_event_id,
            ),
        ),
        clock=GovernanceClock("2042-05-19T16:01:00Z"),
    )

    assert delayed.report is not None
    delayed_outcome = delayed.report.result.portfolio
    assert delayed_outcome is not None
    assert delayed_outcome.disposition == "DENIED"
    assert delayed_outcome.reasons == ("PORTFOLIO_LINEAGE_AFTER_CUTOFF",)


def test_delayed_new_exposure_cannot_bypass_an_effective_tightened_successor(
    migrated_settings: Settings,
) -> None:
    original_proposal = portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "stale-use-original",
            portfolio_confirmation_command(original_proposal),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    successor_proposal = scheduled_portfolio_proposal()
    successor_proposal["risk_budget"]["concentration"].update(
        target_ratio="0.12",
        hard_ratio="0.18",
    )
    successor = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "stale-use-tightened-successor",
            portfolio_confirmation_command(
                successor_proposal,
                previous_authorization_id=original.decision_event_id,
            ),
        ),
        clock=GovernanceClock("2042-05-18T16:00:00Z"),
    )
    assert successor.report is not None
    successor_outcome = successor.report.result.portfolio
    assert successor_outcome is not None
    assert successor_outcome.disposition == "APPROVED"

    def use(requested_action: str) -> Any:
        payload = portfolio_case_payload(
            migrated_settings,
            f"stale-use-{requested_action.lower()}",
            {
                "operation": "PORTFOLIO_USE",
                "portfolio_id": original_proposal["portfolio_id"],
                "authorization_id": original.decision_event_id,
                "requested_action": requested_action,
            },
            account_ids=proposal_account_ids(original_proposal),
        )
        payload["knowledge_cutoff"] = "2042-05-17T16:00:00Z"
        return run_frozen_decision_case(
            migrated_settings,
            payload,
            clock=GovernanceClock("2042-05-19T16:01:00Z"),
        )

    blocked = use("NEW_EXPOSURE")
    assert blocked.report is not None
    blocked_outcome = blocked.report.result.portfolio
    assert blocked_outcome is not None
    assert blocked_outcome.disposition == "DENIED"
    assert blocked_outcome.reasons == ("CURRENT_PORTFOLIO_AUTHORIZATION_REQUIRED",)
    assert blocked_outcome.usage is not None
    assert (
        blocked_outcome.usage.authorization_snapshot.authorization_id == original.decision_event_id
    )

    protected = use("DETERMINISTIC_PROTECTION")
    assert protected.report is not None
    protected_outcome = protected.report.result.portfolio
    assert protected_outcome is not None
    assert protected_outcome.disposition == "APPROVED"
    assert protected_outcome.reasons == ("DETERMINISTIC_PROTECTION_RETAINED",)


def test_overlapping_portfolio_scope_cannot_start_a_new_lineage(
    migrated_settings: Settings,
) -> None:
    original_proposal = portfolio_proposal()
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "scope-rebinding-original",
            portfolio_confirmation_command(original_proposal),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    replacement = scheduled_portfolio_proposal()
    replacement["portfolio_id"] = "synthetic-portfolio-reidentified"
    replacement["risk_budget"]["version_id"] = "synthetic-risk-budget-reidentified"
    replacement["risk_budget"]["concentration"].update(
        target_ratio="0.50",
        hard_ratio="0.90",
    )

    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "scope-rebinding-bypass",
            portfolio_confirmation_command(replacement),
        ),
        clock=GovernanceClock("2042-05-18T16:01:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("PORTFOLIO_ACCOUNT_SCOPE_CONFLICT",)
    assert outcome.authorization is None


def test_portfolio_owner_lineage_filters_owner_and_visibility(
    migrated_settings: Settings,
) -> None:
    payload = portfolio_case_payload(
        migrated_settings,
        "owner-lineage-isolation",
        portfolio_confirmation_command(portfolio_proposal()),
    )
    case = FrozenDecisionCase.model_validate(payload)
    assert case.access_scope is not None
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    ledger = DecisionLedger.from_settings(migrated_settings)

    with ledger.serialize_case_execution() as connection:
        owner_lineage = ledger.portfolio_authorization_owner_lineage(
            connection,
            case.access_scope,
        )
        assert len(owner_lineage) == 1
        assert owner_lineage[0].authorization is not None
        assert owner_lineage[0].authorization.authorization_id == execution.decision_event_id
        assert (
            ledger.portfolio_authorization_owner_lineage(
                connection,
                case.access_scope.model_copy(update={"user_id": "synthetic-other-user"}),
            )
            == ()
        )
        assert (
            ledger.portfolio_authorization_owner_lineage(
                connection,
                case.access_scope.model_copy(update={"visibility": "SHADOW"}),
            )
            == ()
        )


def test_unsupported_activation_account_type_blocks_forward_authorization(
    migrated_settings: Settings,
) -> None:
    original = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "activation-type-original",
            portfolio_confirmation_command(portfolio_proposal()),
        ),
        clock=GovernanceClock(),
    )
    assert original.report is not None
    proposal = scheduled_portfolio_proposal()
    activation = proposal["activation_snapshot"]
    assert isinstance(activation, dict)
    next(
        account
        for account in activation["accounts"]
        if account["account_id"] == "synthetic-account-4017"
    )["account_type"] = "SIMULATED_MARGIN"

    execution = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "activation-type-unsupported",
            portfolio_confirmation_command(
                proposal,
                previous_authorization_id=original.decision_event_id,
            ),
        ),
        clock=GovernanceClock("2042-05-18T16:00:00Z"),
    )

    assert execution.report is not None
    outcome = execution.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("UNSUPPORTED_ACCOUNT_TYPE",)
    assert outcome.authorization is None


def test_insufficient_account_scope_cannot_reveal_another_authorization_snapshot(
    migrated_settings: Settings,
) -> None:
    proposal = next_portfolio_proposal()
    authorized = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "scope-leak-origin",
            portfolio_confirmation_command(deepcopy(proposal)),
        ),
        clock=GovernanceClock("2042-05-19T16:01:00Z"),
    )
    assert authorized.report is not None

    narrow_scope_payload = portfolio_case_payload(
        migrated_settings,
        "scope-leak-denied",
        {
            "operation": "PORTFOLIO_USE",
            "portfolio_id": proposal["portfolio_id"],
            "authorization_id": authorized.decision_event_id,
            "requested_action": "NEW_EXPOSURE",
        },
        account_ids=["synthetic-account-4017"],
    )
    narrow_scope_payload["knowledge_cutoff"] = proposal_activation_cutoff(proposal)
    narrow_scope = run_frozen_decision_case(
        migrated_settings,
        narrow_scope_payload,
        clock=GovernanceClock("2042-05-19T16:02:00Z"),
    )

    assert narrow_scope.report is not None
    outcome = narrow_scope.report.result.portfolio
    assert outcome is not None
    assert outcome.disposition == "DENIED"
    assert outcome.reasons == ("AUTHORIZATION_SCOPE_MISMATCH",)
    assert outcome.preview is None
    assert outcome.usage is None
    saved = get_formal_report(
        narrow_scope.report_version_id,
        migrated_settings,
        principal=AccessPrincipal(
            user_id="stock-profiler-single-user",
            account_ids=("synthetic-account-4017",),
            permissions=("REPORT_READ",),
        ),
    )
    assert saved == narrow_scope.report
    assert "synthetic-account-8029" not in saved.model_dump_json()


def test_expired_authorization_blocks_new_exposure_but_retains_protection_and_obligations(
    migrated_settings: Settings,
) -> None:
    authorized = run_frozen_decision_case(
        migrated_settings,
        portfolio_case_payload(
            migrated_settings,
            "expiry-origin",
            portfolio_confirmation_command(portfolio_proposal()),
        ),
        clock=GovernanceClock(),
    )
    assert authorized.report is not None

    def use(requested_action: str) -> Any:
        payload = portfolio_case_payload(
            migrated_settings,
            f"expiry-{requested_action.lower()}",
            {
                "operation": "PORTFOLIO_USE",
                "portfolio_id": "synthetic-decision-portfolio-alpha",
                "authorization_id": authorized.decision_event_id,
                "requested_action": requested_action,
            },
            account_ids=proposal_account_ids(portfolio_proposal()),
        )
        payload["knowledge_cutoff"] = "2042-11-17T16:00:00Z"
        return run_frozen_decision_case(
            migrated_settings,
            payload,
            clock=GovernanceClock("2042-11-17T16:01:00Z"),
        )

    blocked = use("NEW_EXPOSURE")
    assert blocked.report is not None
    blocked_outcome = blocked.report.result.portfolio
    assert blocked_outcome is not None
    assert blocked_outcome.disposition == "DENIED"
    assert blocked_outcome.reasons == ("RISK_BUDGET_EXPIRED",)
    assert blocked_outcome.usage is not None
    assert blocked_outcome.usage.allowed is False
    assert (
        blocked_outcome.usage.authorization_snapshot.authorization_id
        == authorized.decision_event_id
    )
    assert blocked_outcome.usage.retained_protection_floor.retained_directions == ("REDUCE", "EXIT")
    assert blocked_outcome.usage.unfinished_cash_obligations[0].obligation_id == (
        "synthetic-cash-obligation-alpha"
    )

    protected = use("DETERMINISTIC_PROTECTION")
    assert protected.report is not None
    protected_outcome = protected.report.result.portfolio
    assert protected_outcome is not None
    assert protected_outcome.disposition == "APPROVED"
    assert protected_outcome.reasons == ("DETERMINISTIC_PROTECTION_RETAINED",)
    assert protected_outcome.usage is not None
    assert protected_outcome.usage.allowed is True
    assert protected_outcome.usage.authorization_snapshot == (
        blocked_outcome.usage.authorization_snapshot
    )
