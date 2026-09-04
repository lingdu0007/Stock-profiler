from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "check_license.py"


def load_module() -> object:
    """Load the standalone license gate as a testable module."""
    spec = importlib.util.spec_from_file_location("check_license", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load license checker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_license_policy_rejects_an_unapproved_frontend_license() -> None:
    checker = load_module()

    with pytest.raises(SystemExit, match="unapproved frontend license"):
        checker.require_allowed_frontend_licenses({"Proprietary": []})  # type: ignore[attr-defined]


def test_license_policy_requires_notices_for_direct_runtime_components() -> None:
    checker = load_module()
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")

    checker.require_direct_component_notices(notices)  # type: ignore[attr-defined]
