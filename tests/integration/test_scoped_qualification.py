from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from stock_profiler.bootstrap.decision_cases import (
    get_formal_report,
    run_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case
from stock_profiler.modules.delivery.access import AccessPrincipal


class GovernanceClock:
    def __init__(self, value: str = "2042-05-17T16:01:00Z") -> None:
        self.current = datetime.fromisoformat(value).astimezone(UTC)

    def now(self) -> datetime:
        return self.current


def scope() -> dict[str, Any]:
    return {
        "capability": "synthetic-review",
        "purpose": "synthetic-statistical-use",
        "evidence_level": "D0",
        "user_id": "stock-profiler-single-user",
        "account_ids": ["synthetic-account-4017"],
        "account_type": "SIMULATED_CASH",
        "source": "fictional-ledger-alpha",
        "market_state": "synthetic-steady",
        "board": "synthetic-board",
        "target": "synthetic-review-target",
    }


def version(settings: Settings, name: str = "synthetic-version-one") -> dict[str, Any]:
    return {
        "version_id": name,
        "policy_version": name,
        "implementation": load_frozen_decision_case(settings).version_bundle.model_dump(
            mode="json"
        ),
    }


def qualification_command(
    settings: Settings, *, action: str = "GRANT", previous: str | None = None
) -> dict[str, Any]:
    return {
        "operation": "QUALIFICATION",
        "action": action,
        "scope": scope(),
        "version": version(settings),
        "previous_decision_id": previous,
        "evidence": {
            "evidence_id": "synthetic-qualification-proof-4519",
            "synthetic": True,
            "generator_version": "scoped-governance/1",
            "seed": 4519,
            "version": version(settings),
            "scope": scope(),
            "kind": "QUALIFICATION_PASS",
            "digest": "b" * 64,
            "evaluation_end": "2042-05-01T00:00:00Z",
            "available_at": "2042-05-17T15:30:00Z",
            "expires_at": "2043-05-31T23:59:59Z",
        },
    }


def case_payload(settings: Settings, identity: str, command: dict[str, Any]) -> dict[str, Any]:
    payload = load_frozen_decision_case(settings).model_dump(mode="json")
    payload["business_identity"] = f"synthetic:governance:{identity}"
    payload["case_id"] = f"d0-governance-{identity}"
    payload["version_bundle"].update(
        case_contract_version="4.0.0",
        host_contract_version="4.0.0",
        report_projection_contract_version="4.0.0",
        agent_definition_version="2.0.0",
    )
    payload["agent_definition"]["version"] = "2.0.0"
    payload["access_scope"] = {
        "contract_version": "1.0.0",
        "user_id": "stock-profiler-single-user",
        "account_ids": ["synthetic-account-4017"],
        "visibility": "USER",
    }
    payload["governance"] = command
    return payload


def test_qualification_is_saved_with_exact_scope_and_authorization_evidence(
    migrated_settings: Settings,
) -> None:
    payload = case_payload(migrated_settings, "grant", qualification_command(migrated_settings))
    execution = run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock())
    assert execution.report is not None
    result = execution.report.result.model_dump(mode="json")["governance"]
    assert result["disposition"] == "APPROVED"
    assert result["qualification"]["status"] == "VALID"
    assert result["qualification"]["scope"] == scope()
    assert result["qualification"]["version"] == version(migrated_settings)
    assert result["qualification"]["authorization_id"] == execution.decision_event_id
    assert result["qualification"]["authorization_evidence"]["evidence_id"] == (
        "synthetic-qualification-proof-4519"
    )
    assert result["qualification"]["cause"] == "QUALIFICATION_PASS"
    assert (
        run_frozen_decision_case(migrated_settings, payload, clock=GovernanceClock()).report
        == execution.report
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


def test_repeated_diagnostic_alerts_retain_the_original_authorization(
    migrated_settings: Settings,
) -> None:
    grant = run_frozen_decision_case(
        migrated_settings,
        case_payload(migrated_settings, "grant", qualification_command(migrated_settings)),
        clock=GovernanceClock(),
    )
    previous = grant.decision_event_id
    for identity in ("alert-one", "alert-two"):
        command = qualification_command(migrated_settings, action="ALERT", previous=previous)
        command["evidence"]["kind"] = "DIAGNOSTIC_ALERT"
        command["evidence"]["evidence_id"] = f"synthetic-{identity}"
        alert = run_frozen_decision_case(
            migrated_settings,
            case_payload(migrated_settings, identity, command),
            clock=GovernanceClock(),
        )
        assert alert.report is not None
        record = alert.report.result.model_dump(mode="json")["governance"]["qualification"]
        assert record["status"] == "AT_RISK"
        assert record["authorization_id"] == grant.decision_event_id
        assert record["authorization_evidence"]["kind"] == "QUALIFICATION_PASS"
        assert record["version"] == version(migrated_settings)
        previous = alert.decision_event_id
    assert [item["evidence_id"] for item in record["alerts"]] == [
        "synthetic-alert-one",
        "synthetic-alert-two",
    ]
    assert record["previous_decision_id"] != grant.decision_event_id
    assert grant.report is not None
    assert grant.report.result.model_dump(mode="json")["governance"]["qualification"]["status"] == (
        "VALID"
    )
