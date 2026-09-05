"""Load the sole original, versioned synthetic input admitted by the D0 host."""

from __future__ import annotations

import json
import sysconfig
from pathlib import Path
from typing import Any

FIXTURE_NAME = "replayable_frozen_decision_case.json"


def load_frozen_case_payload() -> dict[str, Any]:
    """Read the reviewed source fixture or its wheel-installed data-file copy."""
    source_fixture = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "synthetic"
    installed_fixture = Path(sysconfig.get_path("data")) / "stock-profiler" / "fixtures"
    for directory in (source_fixture, installed_fixture):
        fixture_path = directory / FIXTURE_NAME
        if fixture_path.is_file():
            payload = json.loads(fixture_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    raise RuntimeError("the replayable frozen decision-case fixture is unavailable")
