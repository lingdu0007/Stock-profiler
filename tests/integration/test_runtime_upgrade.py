from __future__ import annotations

import asyncio
import json
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest
from m_agent.runtime import RunStoreIntegrityError

from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.decision_cases import (
    replay_default_frozen_decision_case,
    run_frozen_decision_case,
)
from stock_profiler.bootstrap.settings import Settings
from stock_profiler.modules.decision_cases.domain import (
    FrozenDecisionCase,
    load_frozen_decision_case,
)

OLD_RELEASE = {
    "m_agent_version": "0.5.0",
    "m_agent_wheel_url": (
        "https://github.com/lingdu0007/M-Agent/releases/download/v0.5.0/"
        "m_agent-0.5.0-py3-none-any.whl"
    ),
    "m_agent_wheel_sha256": "8c2592715e840f5d8da4ce239c663864d0c24a16fa05edcefef09071c4fb59a6",
    "m_agent_release_commit": "743651e5c74a4865f25a31dab68d188b5b0aed64",
}


@pytest.fixture(scope="module")
def historical_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("official-historical-runtime")
    subprocess.run(
        ["uv", "venv", "--python", "3.11", str(directory)], check=True, capture_output=True
    )
    python = directory / "bin" / "python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--no-cache",
            f"{OLD_RELEASE['m_agent_wheel_url']}#sha256={OLD_RELEASE['m_agent_wheel_sha256']}",
            "pydantic==2.13.0",
        ],
        check=True,
        capture_output=True,
    )
    return python


@pytest.mark.parametrize(
    ("mode", "scoped"),
    [
        ("completed", True),
        ("checkpoint", True),
        ("damaged", True),
        ("completed", False),
        ("checkpoint", False),
    ],
)
def test_official_historical_run_keeps_its_identity_through_upgrade(
    migrated_settings: Settings, historical_python: Path, tmp_path: Path, mode: str, scoped: bool
) -> None:
    payload = load_frozen_decision_case(migrated_settings).model_dump(mode="json")
    payload["version_bundle"].update(OLD_RELEASE)
    if scoped:
        payload["version_bundle"].update(
            case_contract_version="3.0.0",
            host_contract_version="3.0.0",
            report_projection_contract_version="3.0.0",
            agent_definition_version="2.0.0",
        )
        payload["agent_definition"]["version"] = "2.0.0"
        payload["access_scope"] = {
            "contract_version": "1.0.0",
            "user_id": "stock-profiler-single-user",
            "account_ids": ["synthetic-account-4017"],
            "visibility": "USER",
        }
    case = FrozenDecisionCase.model_validate(payload)
    envelope = tmp_path / "original-synthetic-case.json"
    envelope.write_text(
        json.dumps(
            {
                "case": payload,
                "run_id": case.framework_run_id,
                "fingerprint": case.frozen_input_fingerprint,
            }
        )
    )
    child = subprocess.run(
        [
            str(historical_python),
            "-I",
            str(Path(__file__).parents[1] / "support" / "legacy_runtime_case.py"),
            str(envelope),
            str(migrated_settings.resolved_m_agent_run_store_path),
            mode,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    original = json.loads(child.stdout)
    assert original["run"]["status"] == ("RUNNING" if mode == "checkpoint" else "SUCCEEDED")
    assert len(original["checkpoints"]) == (2 if scoped else 1)
    if mode == "damaged":
        database = migrated_settings.resolved_m_agent_run_store_path
        before = sha256(database.read_bytes()).hexdigest()
        with pytest.raises(RunStoreIntegrityError):
            run_frozen_decision_case(migrated_settings, payload)
        assert sha256(database.read_bytes()).hexdigest() == before
        return

    execution = run_frozen_decision_case(migrated_settings, payload)

    assert execution.framework_run_id == original["run"]["run_id"] == case.framework_run_id
    assert execution.report is not None
    assert execution.report.version_bundle == case.version_bundle
    assert execution.report.result == case.expected_external_result
    assert execution.framework_run_status == "SUCCEEDED"
    runtime = initialize_runtime_storage(migrated_settings)
    recovered = asyncio.run(runtime.run_store.get_run(case.framework_run_id))
    assert recovered is not None
    assert recovered.model_dump(mode="json")["snapshot"] == original["run"]["snapshot"]
    assert recovered.input == original["run"]["input"]
    assert [
        checkpoint.model_dump(mode="json")
        for checkpoint in asyncio.run(runtime.run_store.get_checkpoints(case.framework_run_id))
    ] == original["checkpoints"]
    assert run_frozen_decision_case(migrated_settings, payload).report == execution.report
    if not scoped:
        assert (
            replay_default_frozen_decision_case(
                migrated_settings, case.business_identity, recovery_case=case
            ).report
            == execution.report
        )
    assert json.loads(envelope.read_text())["case"] == payload
    runtime.run_store.close()
