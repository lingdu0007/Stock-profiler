"""CLI support surface."""

from __future__ import annotations

import argparse
import json

from stock_profiler.bootstrap.settings import load_settings
from stock_profiler.entrypoints.cli.service import (
    diagnostic_snapshot,
    process_health_snapshot,
    version_snapshot,
)


def main() -> None:
    """Render a version or diagnostic snapshot without making an HTTP request."""
    parser = argparse.ArgumentParser(prog="stock-profiler")
    parser.add_argument("command", choices=("version", "doctor", "process-health"))
    parser.add_argument("process", nargs="?", choices=("api", "scheduler", "worker"))
    args = parser.parse_args()
    settings = load_settings()
    if args.command == "version":
        print(json.dumps(version_snapshot(settings), sort_keys=True))
        return
    if args.command == "doctor":
        diagnostic = diagnostic_snapshot(settings)
        print(json.dumps(diagnostic, sort_keys=True))
        if diagnostic["status"] != "ready":
            raise SystemExit(1)
        return
    if args.process is None:
        parser.error("process-health requires api, scheduler, or worker")
    process_health = process_health_snapshot(args.process, settings)
    print(json.dumps(process_health, sort_keys=True))
    if process_health["status"] != "ready":
        raise SystemExit(1)
