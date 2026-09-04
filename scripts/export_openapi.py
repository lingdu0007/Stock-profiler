"""Export the canonical FastAPI OpenAPI 3.1 document."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from stock_profiler.entrypoints.http.app import create_app

ROOT = Path(__file__).resolve().parents[1]
PRETTIER_CONFIG = ROOT / "web" / "prettier.config.mjs"


def format_openapi(output: Path) -> None:
    """Apply the locked frontend formatter so the checked-in contract is stable."""
    subprocess.run(
        [
            "corepack",
            "pnpm@10.17.1",
            "--dir",
            "web",
            "exec",
            "prettier",
            "--config",
            str(PRETTIER_CONFIG),
            "--write",
            str(output.resolve()),
        ],
        cwd=ROOT,
        check=True,
    )


def main() -> None:
    """Write a stable, newline-terminated OpenAPI JSON document."""
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    format_openapi(args.output)


if __name__ == "__main__":
    main()
