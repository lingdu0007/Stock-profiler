from __future__ import annotations

import json
import sys

import pytest

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.cli import main
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case


def test_cli_runs_and_replays_the_frozen_case_by_its_stable_business_identity(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    case = load_frozen_decision_case()
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
