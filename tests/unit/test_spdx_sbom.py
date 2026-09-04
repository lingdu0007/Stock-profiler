from __future__ import annotations

import importlib.util
from pathlib import Path

from pytest import MonkeyPatch

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "generate_spdx_sbom.py"


def load_module() -> object:
    """Load the standalone build script as a testable module."""
    spec = importlib.util.spec_from_file_location("generate_spdx_sbom", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load SBOM generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_spdx_sbom_has_unique_ids_and_all_locked_build_inputs(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("STOCK_PROFILER_SOURCE_SHA", "a" * 40)
    payload = load_module().build_payload()  # type: ignore[attr-defined]
    packages = payload["packages"]
    identifiers = [package["SPDXID"] for package in packages]
    names = {package["name"] for package in packages}
    oci_packages = [
        package
        for package in packages
        if package["name"] in {"python:3.11.15-slim", "caddy:2.10.2-alpine"}
    ]
    licenses = {package["name"]: package["licenseDeclared"] for package in packages}

    assert len(identifiers) == len(set(identifiers))
    assert {"fastapi", "react", "python:3.11.15-slim", "caddy:2.10.2-alpine"} <= names
    assert payload["documentNamespace"].endswith("-" + "a" * 40)
    assert licenses["m-agent"] == "Apache-2.0"
    assert licenses["@simplewebauthn/browser"] == "MIT"
    assert not {
        "Apache 2.0",
        "Apache License, Version 2.0",
        "BSD",
        "ISC License",
    } & set(licenses.values())
    assert all(package["downloadLocation"] == "NOASSERTION" for package in oci_packages)
