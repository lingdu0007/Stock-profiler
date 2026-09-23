from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from test_authenticated_report_delivery import _authenticated_client_with_csrf
from test_research_risk_veto import _case

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.cli import main


def test_cli_runs_and_replays_the_ticket16_research_case(
    migrated_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    case = _case(migrated_settings)
    case_path = tmp_path / "ticket16-research-case.json"
    case_path.write_text(json.dumps(case.model_dump(mode="json")), encoding="utf-8")
    monkeypatch.setattr("stock_profiler.entrypoints.cli.load_settings", lambda: migrated_settings)

    monkeypatch.setattr(
        sys,
        "argv",
        ["stock-profiler", "decision-case-run", "--case", str(case_path)],
    )
    main()
    first = json.loads(capsys.readouterr().out)

    assert first["business_result_status"] == "REJECTED"
    assert first["report"]["result"]["outcome_code"] == "RESEARCH_REJECTED"
    assert first["report"]["result"]["research"]["risk_veto"]["disposition"] == "REJECTED"
    assert (
        first["report"]["framework_run_id"]
        != first["report"]["result"]["research"]["handoff"]["risk_run_id"]
    )
    assert {
        stage["phase"]: stage["status"]
        for stage in first["report"]["stage_results"]
        if stage["phase"] in {"RESEARCH", "RISK_VETO", "BUSINESS_DECISION"}
    } == {
        "RESEARCH": "SUCCEEDED",
        "RISK_VETO": "REJECTED",
        "BUSINESS_DECISION": "REJECTED",
    }
    assert first["publication_status"] == "PUBLISHED"
    assert first["report_version_id"] == case.report_version_id

    monkeypatch.setattr(
        sys,
        "argv",
        ["stock-profiler", "decision-case-replay", "--business-identity", case.business_identity],
    )
    main()
    replay = json.loads(capsys.readouterr().out)

    assert replay == first


def test_authenticated_api_projects_the_ticket16_research_report(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _case(migrated_settings, user_id="stock-profiler-single-user")
    execution = run_frozen_decision_case(migrated_settings, case.model_dump(mode="json"))
    assert execution.report is not None
    client, _ = _authenticated_client_with_csrf(migrated_settings, monkeypatch)

    response = client.get(f"/api/v1/reports/{execution.report.report_version_id}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == execution.report.model_dump(mode="json")
    assert response.json()["result"]["outcome_code"] == "RESEARCH_REJECTED"
    assert response.json()["result"]["research"]["risk_veto"]["disposition"] == "REJECTED"
    assert response.json()["event_id"] == execution.decision_event_id
    assert response.json()["framework_run_id"] == execution.framework_run_id
