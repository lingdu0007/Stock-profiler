from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case


@pytest.fixture
def security_cases() -> dict[str, Any]:
    path = Path(__file__).parents[1] / "fixtures/synthetic/read_only_security_cases.json"
    payload: dict[str, Any] = json.loads(path.read_text())
    assert payload["synthetic"] is True
    assert payload["generator_version"] == "original-read-only-security-v1"
    assert payload["seed"] == 4519
    return payload


@pytest.fixture
def scoped_payload(migrated_settings: Settings, security_cases: dict[str, Any]) -> dict[str, Any]:
    payload = load_frozen_decision_case(migrated_settings).model_dump(mode="json")
    payload["access_scope"] = {
        "contract_version": "1.0.0",
        "user_id": security_cases["user_id"],
        "account_ids": security_cases["account_ids"],
        "visibility": "USER",
    }
    payload["version_bundle"].update(
        case_contract_version="3.0.0",
        host_contract_version="3.0.0",
        report_projection_contract_version="3.0.0",
        agent_definition_version="2.0.0",
    )
    payload["agent_definition"]["version"] = "2.0.0"
    return payload
