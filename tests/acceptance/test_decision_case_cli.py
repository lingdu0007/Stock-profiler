from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from stock_profiler.adapters.persistence.decision_ledger import DecisionLedger
from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.cli import main
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case


def test_cli_runs_and_replays_the_frozen_case_by_its_stable_business_identity(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: migrated_settings)

    monkeypatch.setattr(sys, "argv", ["stock-profiler", "decision-case-run"])
    main()
    first = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        sys,
        "argv",
        ["stock-profiler", "decision-case-replay", "--business-identity", case.business_identity],
    )
    main()
    replay = json.loads(capsys.readouterr().out)

    assert replay == first
    assert replay["framework_run_status"] == "SUCCEEDED"
    assert replay["business_commit_status"] == "COMMITTED"
    assert replay["publication_status"] == "PUBLISHED"
    assert replay["business_object_id"] == case.business_object_id
    assert replay["framework_run_id"] == case.framework_run_id
    assert replay["decision_event_id"] == case.decision_event_id
    assert replay["report_version_id"] == case.report_version_id


def test_cli_replays_the_same_append_only_correction_by_business_identity(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    case = load_frozen_decision_case(migrated_settings)
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: migrated_settings)

    monkeypatch.setattr(sys, "argv", ["stock-profiler", "decision-case-run"])
    main()
    capsys.readouterr()

    monkeypatch.setattr(
        sys,
        "argv",
        ["stock-profiler", "decision-case-correct", "--business-identity", case.business_identity],
    )
    main()
    first = json.loads(capsys.readouterr().out)

    main()
    second = json.loads(capsys.readouterr().out)

    assert second == first
    assert first["original_event_id"] == case.decision_event_id
    assert first["report"]["corrects_event_id"] == case.decision_event_id
    assert first["report"]["framework_run_id"] == case.framework_run_id


def test_cli_replay_rejects_an_unknown_business_identity(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: migrated_settings)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stock-profiler",
            "decision-case-replay",
            "--business-identity",
            "synthetic:decision:unknown:999",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        main()


def test_cli_rejects_a_versioned_case_for_a_command_other_than_run(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: migrated_settings)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "stock-profiler",
            "decision-case-replay",
            "--business-identity",
            "synthetic:decision:orbital-mosaic:001",
            "--case",
            "/tmp/synthetic-case.json",
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        main()


@pytest.mark.parametrize(
    ("invalid_field", "invalid_value"),
    [
        pytest.param(
            "recovery_framework_run_id",
            "synthetic-original-run",
            id="recovery-marked",
        ),
        pytest.param(
            "host_application_version",
            "synthetic-uninstalled-version",
            id="incorrect-host-application-version",
        ),
    ],
)
def test_cli_rejects_an_invalid_host_case_before_creating_any_result(
    migrated_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_field: str,
    invalid_value: str,
) -> None:
    case_payload = load_frozen_decision_case(migrated_settings).model_dump(mode="json")
    if invalid_field == "host_application_version":
        version_bundle = case_payload["version_bundle"]
        assert isinstance(version_bundle, dict)
        version_bundle[invalid_field] = invalid_value
    else:
        case_payload[invalid_field] = invalid_value
    case_path = tmp_path / f"synthetic-{invalid_field}-case.json"
    case_path.write_text(json.dumps(case_payload), encoding="utf-8")
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: migrated_settings)
    monkeypatch.setattr(
        sys,
        "argv",
        ["stock-profiler", "decision-case-run", "--case", str(case_path)],
    )

    with pytest.raises(SystemExit, match="2"):
        main()

    assert DecisionLedger.from_settings(migrated_settings).counts() == {
        "business_objects": 0,
        "decision_events": 0,
        "reports": 0,
    }
    assert [
        (fact.surface, fact.reason)
        for fact in ResultDelivery.from_settings(migrated_settings).audit_history()
    ] == [
        ("HOST", "UNDECLARED_CAPABILITY"),
        ("CLI", "UNDECLARED_CAPABILITY"),
    ]


def test_cli_creates_a_short_lived_host_console_grant_without_an_http_request(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = migrated_settings.model_copy(
        update={
            "auth_bootstrap_token": SecretStr("bootstrap-token-for-synthetic-test"),
            "auth_recovery_token": SecretStr("recovery-token-for-synthetic-test"),
        }
    )
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: settings)
    monkeypatch.setattr(
        sys,
        "argv",
        ["stock-profiler", "host-console-grant", "--purpose", "bootstrap"],
    )

    main()

    grant = json.loads(capsys.readouterr().out)
    assert grant["purpose"] == "bootstrap"
    assert isinstance(grant["grant_id"], str)
    assert grant["grant_id"]
    assert grant["enrollment_path"] == f"/enroll#{grant['grant_id']}"
