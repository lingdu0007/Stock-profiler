"""CLI support surface."""

from __future__ import annotations

import argparse
import json

from stock_profiler.adapters.authentication.passkeys import (
    AuthenticationError,
    HostGrantPurpose,
    PasskeyAuthenticator,
)
from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage
from stock_profiler.bootstrap.settings import load_settings
from stock_profiler.entrypoints.cli.service import (
    diagnostic_snapshot,
    process_health_snapshot,
    version_snapshot,
)
from stock_profiler.modules.decision_cases.service import (
    correct_default_frozen_decision_case,
    replay_default_frozen_decision_case,
    run_default_frozen_decision_case,
)


def main() -> None:
    """Render a version or diagnostic snapshot without making an HTTP request."""
    parser = argparse.ArgumentParser(prog="stock-profiler")
    parser.add_argument(
        "command",
        choices=(
            "version",
            "doctor",
            "process-health",
            "decision-case-run",
            "decision-case-replay",
            "decision-case-correct",
            "host-console-grant",
        ),
    )
    parser.add_argument("process", nargs="?", choices=("api", "scheduler", "worker"))
    parser.add_argument("--business-identity")
    parser.add_argument("--purpose", choices=("bootstrap", "recovery"))
    args = parser.parse_args()
    settings = load_settings()
    if args.command == "version":
        print(json.dumps(version_snapshot(settings), sort_keys=True))
        return
    if args.command == "decision-case-run":
        print(run_default_frozen_decision_case(settings).model_dump_json())
        return
    if args.command == "decision-case-replay":
        if args.business_identity is None:
            parser.error("decision-case-replay requires --business-identity")
        try:
            execution = replay_default_frozen_decision_case(settings, args.business_identity)
        except ValueError as error:
            parser.error(str(error))
        print(execution.model_dump_json())
        return
    if args.command == "decision-case-correct":
        if args.business_identity is None:
            parser.error("decision-case-correct requires --business-identity")
        try:
            correction = correct_default_frozen_decision_case(settings, args.business_identity)
        except ValueError as error:
            parser.error(str(error))
        print(correction.model_dump_json())
        return
    if args.command == "host-console-grant":
        if args.purpose is None:
            parser.error("host-console-grant requires --purpose")
        try:
            grant_id = PasskeyAuthenticator(
                initialize_runtime_storage(settings).engine, settings
            ).create_host_console_grant(HostGrantPurpose(args.purpose))
        except AuthenticationError as error:
            parser.error(str(error))
        print(
            json.dumps(
                {
                    "enrollment_path": f"/enroll#{grant_id}",
                    "grant_id": grant_id,
                    "purpose": args.purpose,
                },
                sort_keys=True,
            )
        )
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
