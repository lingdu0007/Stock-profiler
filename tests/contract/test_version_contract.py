from __future__ import annotations

import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from stock_profiler.entrypoints.http.app import create_app
from stock_profiler.foundation.versioning import (
    M_AGENT_WHEEL_SHA256,
    M_AGENT_WHEEL_URL,
    build_version_bundle,
)

ROOT = Path(__file__).resolve().parents[2]


def test_http_openapi_and_diagnostic_contract_share_one_version_bundle(settings: object) -> None:
    bundle = build_version_bundle(settings)  # type: ignore[arg-type]
    response = TestClient(create_app(settings)).get("/api/v1/diagnostics/version")  # type: ignore[arg-type]

    assert response.status_code == 200
    assert response.json() == bundle.to_dto()
    assert create_app(settings).openapi()["openapi"] == "3.1.0"  # type: ignore[arg-type]
    assert bundle.application_version == "0.1.0.dev0"
    assert bundle.source_sha == "a" * 40
    assert bundle.m_agent_version == "0.5.1"
    assert bundle.m_agent_wheel_sha256 == (
        "7528e768c36890d4005c90f2f24a97ae96714b96f8d8c5655542e6289004fbaa"
    )
    assert bundle.m_agent_release_commit == "99dd386b6f2c93645334ec81c9791f3b0333d597"


def test_m_agent_diagnostic_identity_matches_the_locked_project_dependency() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert f"m-agent @ {M_AGENT_WHEEL_URL}#sha256={M_AGENT_WHEEL_SHA256}" in dependencies
