"""Verify the public license and third-party notice baseline."""

from __future__ import annotations

import json
import subprocess
import tomllib
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_DIRECT_COMPONENTS = frozenset(
    {
        "alembic",
        "fastapi",
        "m-agent",
        "pydantic-settings",
        "sqlalchemy",
        "structlog",
        "typer",
        "uvicorn",
        "webauthn",
    }
)
NOTICE_COMPONENT_MARKERS = (
    "M-Agent",
    "FastAPI",
    "SQLAlchemy",
    "Alembic",
    "Pydantic",
    "structlog",
    "Typer",
    "Uvicorn",
    "webauthn",
    "@simplewebauthn/browser",
    "React",
    "Vite",
    "TypeScript",
)
ALLOWED_PYTHON_LICENSES = frozenset(
    {
        "Apache-2.0",
        "BSD-3-Clause",
        "MIT",
        "MIT OR Apache-2.0",
    }
)
ALLOWED_FRONTEND_LICENSES = frozenset(
    {
        "(MIT OR CC0-1.0)",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "BlueOak-1.0.0",
        "CC-BY-4.0",
        "ISC",
        "MIT",
        "MPL-2.0",
        "Python-2.0",
    }
)


def require_text(path: Path, marker: str) -> None:
    """Require an expected marker in a required public legal document."""
    if marker not in path.read_text(encoding="utf-8"):
        raise SystemExit(f"missing {marker!r} in {path.relative_to(ROOT)}")


def locked_python_dependencies() -> set[str]:
    """Return the direct runtime dependencies recorded in the immutable Python lock."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    root_package = next(
        package for package in lock["package"] if package["name"] == "stock-profiler"
    )
    return {dependency["name"] for dependency in root_package["dependencies"]}


def require_python_license_metadata() -> None:
    """Require approved SPDX-compatible metadata for each direct runtime dependency."""
    locked = locked_python_dependencies()
    if not locked >= PYTHON_DIRECT_COMPONENTS:
        missing = ", ".join(sorted(PYTHON_DIRECT_COMPONENTS - locked))
        raise SystemExit(f"direct Python dependencies missing from lock: {missing}")
    for component in sorted(PYTHON_DIRECT_COMPONENTS):
        try:
            component_metadata = metadata(component)
        except PackageNotFoundError as error:
            message = f"installed Python dependency missing metadata: {component}"
            raise SystemExit(message) from error
        license_expression = (
            component_metadata.get("License-Expression") or component_metadata.get("License") or ""
        )
        if license_expression not in ALLOWED_PYTHON_LICENSES:
            raise SystemExit(f"unapproved Python license for {component}: {license_expression!r}")


def frontend_license_inventory() -> dict[str, list[dict[str, object]]]:
    """Read pnpm's resolved license inventory rather than trusting package ranges."""
    completed = subprocess.run(
        ["corepack", "pnpm@10.17.1", "--dir", "web", "licenses", "list", "--json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def require_allowed_frontend_licenses(licenses: dict[str, list[dict[str, object]]]) -> None:
    """Reject every resolved frontend package outside the approved license policy."""
    unapproved = sorted(set(licenses) - ALLOWED_FRONTEND_LICENSES)
    if unapproved:
        raise SystemExit(f"unapproved frontend license: {', '.join(unapproved)}")


def require_direct_component_notices(notices: str) -> None:
    """Keep legal notices synchronized with the direct runtime and Web components."""
    missing = [marker for marker in NOTICE_COMPONENT_MARKERS if marker not in notices]
    if missing:
        raise SystemExit(f"third-party notices missing direct components: {', '.join(missing)}")


def require_frontend_lock_coverage(licenses: dict[str, list[dict[str, object]]]) -> None:
    """Ensure each direct Web dependency resolves in the inspected pnpm graph."""
    package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    direct_dependencies = set(package["dependencies"]) | set(package["devDependencies"])
    resolved_names = {
        str(component["name"]) for components in licenses.values() for component in components
    }
    missing = sorted(direct_dependencies - resolved_names)
    if missing:
        message = f"direct frontend dependencies missing from lock inventory: {', '.join(missing)}"
        raise SystemExit(message)


def main() -> None:
    """Check legal documents, locked direct dependencies, and license policy."""
    require_text(ROOT / "LICENSE", "Apache License")
    require_text(ROOT / "NOTICE", "Stock Profiler")
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    require_direct_component_notices(notices)
    require_python_license_metadata()
    frontend_licenses = frontend_license_inventory()
    require_allowed_frontend_licenses(frontend_licenses)
    require_frontend_lock_coverage(frontend_licenses)
    print("license check passed")


if __name__ == "__main__":
    main()
