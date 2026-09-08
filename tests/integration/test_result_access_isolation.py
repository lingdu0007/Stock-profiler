from __future__ import annotations

import pytest

from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.decision_cases import (
    get_formal_report,
    run_default_frozen_decision_case,
    run_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case
from stock_profiler.modules.delivery.access import AccessPrincipal


def test_report_read_without_an_explicit_principal_fails_closed(
    migrated_settings: Settings,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    assert execution.report is not None

    assert get_formal_report(execution.report_version_id, migrated_settings) is None


@pytest.mark.parametrize(
    ("user_id", "account_ids", "permissions", "reason"),
    [
        ("other-user", ("synthetic-account-4017",), ("REPORT_READ",), "USER_SCOPE"),
        ("stock-profiler-single-user", ("other-account",), ("REPORT_READ",), "ACCOUNT_SCOPE"),
        ("stock-profiler-single-user", ("synthetic-account-4017",), (), "PERMISSION"),
    ],
)
def test_scope_denials_are_durable_without_disclosing_result_content(
    migrated_settings: Settings,
    user_id: str,
    account_ids: tuple[str, ...],
    permissions: tuple[str, ...],
    reason: str,
) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    principal = AccessPrincipal(user_id=user_id, account_ids=account_ids, permissions=permissions)

    assert (
        get_formal_report(execution.report_version_id, migrated_settings, principal=principal)
        is None
    ), f"PROTECTION_CONTRACT_{reason}"
    facts = ResultDelivery.from_settings(migrated_settings).audit_history()
    assert len(facts) == 1
    assert facts[0].reason == reason
    assert facts[0].outcome == "DENIED"
    serialized = facts[0].model_dump_json()
    assert "Orbital" not in serialized
    assert "XQZ-4017" not in serialized
    assert user_id not in serialized


def test_authorized_owner_reads_the_exact_saved_report(migrated_settings: Settings) -> None:
    execution = run_default_frozen_decision_case(migrated_settings)
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ",),
    )
    assert (
        get_formal_report(execution.report_version_id, migrated_settings, principal=principal)
        == execution.report
    )


def test_shadow_scope_is_frozen_and_cannot_publish_a_user_report(
    migrated_settings: Settings,
) -> None:
    payload = load_frozen_decision_case(migrated_settings).model_dump(mode="json")
    payload["business_identity"] = "synthetic:decision:isolated-shadow:001"
    payload["case_id"] = "d0-isolated-shadow-001"
    payload["access_scope"] = {
        "contract_version": "1.0.0",
        "user_id": "stock-profiler-single-user",
        "account_ids": ["synthetic-account-4017"],
        "visibility": "SHADOW",
    }
    payload["version_bundle"].update(
        case_contract_version="3.0.0",
        host_contract_version="3.0.0",
        report_projection_contract_version="3.0.0",
        agent_definition_version="2.0.0",
    )
    payload["agent_definition"]["version"] = "2.0.0"

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.business_commit_status == "COMMITTED"
    assert execution.publication_status == "CLOSED"
    assert execution.report is None
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
        is None
    )


def test_owner_is_bound_to_the_result_not_just_the_current_session(
    migrated_settings: Settings,
) -> None:
    payload = load_frozen_decision_case(migrated_settings).model_dump(mode="json")
    payload["access_scope"] = {
        "contract_version": "1.0.0",
        "user_id": "synthetic-other-owner",
        "account_ids": ["synthetic-account-4017"],
        "visibility": "USER",
    }
    payload["version_bundle"].update(
        case_contract_version="3.0.0",
        host_contract_version="3.0.0",
        report_projection_contract_version="3.0.0",
        agent_definition_version="2.0.0",
    )
    payload["agent_definition"]["version"] = "2.0.0"
    execution = run_frozen_decision_case(migrated_settings, payload)
    assert execution.report is not None
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
        is None
    )
