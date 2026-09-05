"""Write the artifact version bundle from committed locks and runtime identity."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from stock_profiler.adapters.persistence.runtime_ownership import APPLICATION_SCHEMA_REVISION
from stock_profiler.bootstrap.settings import load_settings
from stock_profiler.foundation.versioning import build_version_bundle

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    """Return a stable digest for a committed lockfile."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    """Write a source and dependency identity record for one controlled build."""
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = {
        "version": build_version_bundle(load_settings()).to_dto(),
        "python_lock_sha256": sha256(ROOT / "uv.lock"),
        "frontend_lock_sha256": sha256(ROOT / "web" / "pnpm-lock.yaml"),
        "schema_revision": APPLICATION_SCHEMA_REVISION,
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
