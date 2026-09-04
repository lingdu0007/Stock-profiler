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
    assert bundle.m_agent_version == "0.5.0"
    assert bundle.m_agent_wheel_sha256 == (
        "8c2592715e840f5d8da4ce239c663864d0c24a16fa05edcefef09071c4fb59a6"
    )
    assert bundle.m_agent_release_commit == "743651e5c74a4865f25a31dab68d188b5b0aed64"


def test_m_agent_diagnostic_identity_matches_the_locked_project_dependency() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert f"m-agent @ {M_AGENT_WHEEL_URL}#sha256={M_AGENT_WHEEL_SHA256}" in dependencies
