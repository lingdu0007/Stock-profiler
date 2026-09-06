from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from stock_profiler.adapters.authentication.passkeys import PasskeyAuthenticator
from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.http.app import create_app
from stock_profiler.modules.delivery.access import AccessPrincipal, read_denial
from stock_profiler.modules.delivery.user_facts import UserFactRequest


@pytest.mark.parametrize("visibility", ["USER", "SHADOW"])
def test_closed_derived_surfaces_and_order_payloads_never_disclose_saved_content(
    migrated_settings: Settings,
    scoped_payload: dict[str, Any],
    security_cases: dict[str, Any],
    visibility: str,
) -> None:
    scoped_payload["access_scope"]["visibility"] = visibility
    settings = migrated_settings.model_copy(
        update={
            "auth_origin": "https://localhost",
            "report_permissions": ("REPORT_READ", "USER_FACT"),
        }
    )
    execution = run_frozen_decision_case(settings, scoped_payload)
    # Synthetic durable session setup; separate passkey tests exercise the ceremonies.
    token, csrf = PasskeyAuthenticator(
        initialize_runtime_storage(settings).engine, settings
    )._create_session("synthetic-security-passkey")
    client = TestClient(create_app(settings), base_url="https://localhost")
    client.cookies.set(settings.auth_session_cookie_name, token)
    prefix = f"/api/v1/reports/{execution.report_version_id}"
    if visibility == "SHADOW":
        assert client.get(prefix).status_code == 404
        assert client.get(prefix + "/facts").status_code == 404
    for surface in security_cases["closed_surfaces"]:
        for method in ("GET", "POST"):
            response = client.request(
                method, prefix + "/" + surface, json=security_cases["forbidden_payload"]
            )
            assert response.status_code == 404
            assert response.json() == {"detail": "Not Found"}
            assert response.headers["cache-control"] == "no-store"
    response = client.post(
        prefix + "/facts",
        headers={"Origin": "https://localhost", "X-CSRF-Token": csrf},
        json={
            "kind": "CONFIRMED",
            "choice": "ACCEPT",
            "idempotency_key": "synthetic-injected",
            **security_cases["forbidden_payload"],
        },
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "request is not permitted"}
    audit = ResultDelivery.from_settings(settings).audit_history()
    assert len(audit) == 9 + (2 if visibility == "SHADOW" else 0)
    assert all("XQZ-4017" not in fact.model_dump_json() for fact in audit)


def test_shadow_policy_denies_even_all_grants_and_fact_shapes_are_disjoint() -> None:
    principal = AccessPrincipal(
        user_id="stock-profiler-single-user",
        account_ids=("synthetic-account-4017",),
        permissions=("REPORT_READ", "USER_FACT"),
    )
    assert read_denial(principal, principal.account_ids, shadow=True) == "SHADOW_ISOLATED"
    assert read_denial(principal, (), permission="USER_FACT") == "ACCOUNT_SCOPE"
    for payload in (
        {"kind": "CONFIRMED"},
        {"kind": "VIEWED", "choice": "ACCEPT"},
        {"kind": "EXECUTION_DECLARED"},
        {"kind": "ACKNOWLEDGED", "declaration": "REPORTED_FILLED"},
    ):
        with pytest.raises(ValueError):
            UserFactRequest.model_validate({"idempotency_key": "synthetic-invalid", **payload})
