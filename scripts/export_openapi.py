"""Export the canonical FastAPI OpenAPI 3.1 document."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from stock_profiler.entrypoints.http.app import create_app


def main() -> None:
    """Write a stable, newline-terminated OpenAPI JSON document."""
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
