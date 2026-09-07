from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any

import pytest
from test_scoped_qualification import GovernanceClock

from stock_profiler.bootstrap.decision_cases import get_formal_report, run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case
from stock_profiler.modules.delivery.access import AccessPrincipal


def portfolio_proposal() -> dict[str, Any]:
    cutoff = "2042-05-17T16:00:00Z"
    return {
        "portfolio_id": "synthetic-decision-portfolio-alpha",
        "snapshot": {
            "snapshot_id": "synthetic-portfolio-snapshot-alpha",
            "cutoff_at": cutoff,
            "accounts": [
                {
                    "account_id": "synthetic-account-4017",
                    "account_type": "SIMULATED_CASH",
                    "scope": "FULL_ACCOUNT",
                    "captured_at": cutoff,
                    "cash_fact_id": "synthetic-cash-fact-4017",
                    "positions_fact_id": "synthetic-positions-fact-4017",
                    "receivables_fact_id": "synthetic-receivables-fact-4017",
                    "payables_fact_id": "synthetic-payables-fact-4017",
                    "unfinished_trades_fact_id": "synthetic-trades-fact-4017",
                },
                {
                    "account_id": "synthetic-account-margin-2001",
                    "account_type": "SIMULATED_MARGIN",
                    "scope": "FULL_ACCOUNT",
                    "captured_at": cutoff,
                    "cash_fact_id": "synthetic-cash-fact-margin-2001",
                    "positions_fact_id": "synthetic-positions-fact-margin-2001",
                    "receivables_fact_id": "synthetic-receivables-fact-margin-2001",
                    "payables_fact_id": "synthetic-payables-fact-margin-2001",
                    "unfinished_trades_fact_id": "synthetic-trades-fact-margin-2001",
                },
            ],
            "selected_account_ids": ["synthetic-account-4017"],
        },
        "risk_budget": {
            "contract_version": "1.0.0",
            "version_id": "synthetic-risk-budget-alpha",
            "synthetic": True,
            "generator_version": "portfolio-authorization/1",
            "seed": 6101,
            "effective_at": cutoff,
            "expires_at": "2042-11-17T16:00:00Z",
            "concentration": {"target_ratio": "0.13", "hard_ratio": "0.19"},
            "stress": {"target_ratio": "0.14", "hard_ratio": "0.18"},
            "cash": {"target_ratio": "0.39", "hard_ratio": "0.23"},
            "drawdown": {
                "caution_ratio": "0.11",
                "defensive_ratio": "0.16",
                "preservation_ratio": "0.21",
            },
            "downside_grid": ["0.04", "0.09"],
            "protection_floor": {
                "new_exposure_blocked": True,
                "retained_directions": ["REDUCE", "EXIT"],
            },
        },
        "cash_obligations": [
            {
                "obligation_id": "synthetic-cash-obligation-alpha",
                "amount": "125.50",
                "purpose": "synthetic-near-term-liquidity",
                "latest_usable_at": "2042-08-17T16:00:00Z",
                "target_account_id": "synthetic-account-4017",
            }
        ],
    }


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
    selected = (
        snapshot.get("selected_account_ids", [])
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
        "account_ids": selected,
        "visibility": "USER",
    }
    payload["portfolio"] = command
    return payload


def portfolio_confirmation_command(
    proposal: dict[str, Any], *, previous_authorization_id: str | None = None
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
            "confirmed_at": proposal["risk_budget"]["effective_at"],
            "confirmed": True,
        },
    }


def next_portfolio_proposal() -> dict[str, Any]:
    proposal = portfolio_proposal()
    cutoff = "2042-05-18T16:00:00Z"
    proposal["snapshot"]["snapshot_id"] = "synthetic-portfolio-snapshot-beta"
    proposal["snapshot"]["cutoff_at"] = cutoff
    for account in proposal["snapshot"]["accounts"]:
        account["captured_at"] = cutoff
    proposal["snapshot"]["accounts"].append(
        {
            "account_id": "synthetic-account-8029",
            "account_type": "SIMULATED_CASH",
            "scope": "FULL_ACCOUNT",
            "captured_at": cutoff,
            "cash_fact_id": "synthetic-cash-fact-8029",
            "positions_fact_id": "synthetic-positions-fact-8029",
            "receivables_fact_id": "synthetic-receivables-fact-8029",
            "payables_fact_id": "synthetic-payables-fact-8029",
            "unfinished_trades_fact_id": "synthetic-trades-fact-8029",
        }
    )
    proposal["snapshot"]["selected_account_ids"].append("synthetic-account-8029")
    proposal["risk_budget"].update(
        version_id="synthetic-risk-budget-beta",
        effective_at=cutoff,
        expires_at="2042-11-18T16:00:00Z",
    )
    return proposal


def reduced_portfolio_proposal() -> dict[str, Any]:
    proposal = next_portfolio_proposal()
    cutoff = "2042-05-19T16:00:00Z"
    account = proposal["snapshot"]["accounts"][0]
    account["captured_at"] = cutoff
    account["cash_fact_id"] = "synthetic-cash-fact-4017-funded-gamma"
    proposal["snapshot"].update(
        snapshot_id="synthetic-portfolio-snapshot-gamma",
        cutoff_at=cutoff,
        accounts=[account],
        selected_account_ids=["synthetic-account-4017"],
    )
    proposal["risk_budget"].update(
        version_id="synthetic-risk-budget-gamma",
        effective_at=cutoff,
        expires_at="2042-11-19T16:00:00Z",
    )
    proposal["cash_obligations"][0]["amount"] = "95.25"
    return proposal


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
                account_ids=("synthetic-account-4017",),
                permissions=("REPORT_READ",),
            ),
        )
        == execution.report
    )


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
        clock=GovernanceClock("2042-05-18T16:01:00Z"),
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
        clock=GovernanceClock("2042-05-18T16:02:00Z"),
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
        clock=GovernanceClock("2042-05-19T16:02:00Z"),
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
        "synthetic-account-4017",
    )
    assert reduced_authorization.proposal.snapshot.accounts[0].cash_fact_id == (
        "synthetic-cash-fact-4017-funded-gamma"
    )
    assert reduced_authorization.proposal.cash_obligations[0].amount == Decimal("95.25")
    assert replacement.report.model_dump_json() == replacement_report


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
            account_ids=["synthetic-account-4017"],
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
