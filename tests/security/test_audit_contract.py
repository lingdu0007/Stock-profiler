from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from stock_profiler.adapters.m_agent import frozen_decision_case as adapter
from stock_profiler.adapters.persistence.result_delivery import ResultDelivery
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.entrypoints.http.app import create_app
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case


class FrozenClock:
    def now(self) -> datetime:
        return datetime(2042, 5, 17, 16, 2, tzinfo=UTC)


def test_http_report_and_framework_denials_share_the_injected_clock(
    migrated_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FrozenClock()
    client = TestClient(create_app(migrated_settings, clock=clock))
    assert client.get("/api/v1/reports/synthetic-missing").status_code == 401
    assert ResultDelivery.from_settings(migrated_settings).audit_history()[-1].recorded_at == (
        "2042-05-17T16:02:00Z"
    )
    case = load_frozen_decision_case(migrated_settings)
    runtime = initialize_runtime_storage(migrated_settings)
    original = adapter._frozen_definition(case)
    monkeypatch.setattr(
        adapter,
        "_frozen_definition",
        lambda _: original.model_copy(update={"model_adapters": {"forged": object()}}),
    )
    with pytest.raises(ValueError, match="capability"):
        asyncio.run(adapter.execute_frozen_decision_case(case, runtime, clock=clock))
    assert ResultDelivery.from_settings(migrated_settings).audit_history()[-1].recorded_at == (
        "2042-05-17T16:02:00Z"
    )


def test_opaque_validation_response_matches_the_published_contract(
    migrated_settings: Settings,
) -> None:
    app = create_app(migrated_settings)
    schema = app.openapi()
    response = schema["paths"]["/api/v1/reports/{report_version_id}/facts"]["post"]["responses"][
        "422"
    ]
    assert response["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/OpaqueRequestErrorDto"
    )
