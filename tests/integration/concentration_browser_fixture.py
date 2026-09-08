"""Generate an independent historical D0 input for the real browser CLI journey."""

import json
import sys
from pathlib import Path
from typing import Any

from test_issuer_concentration import (
    concentration_authorization_payload,
    concentration_payload,
)

from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case
from stock_profiler.bootstrap.settings import load_settings


def historical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: historical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [historical(item) for item in value]
    if isinstance(value, str) and value.startswith("2042-"):
        return "2025-" + value[5:]
    if value == "synthetic-account-4017":
        return "synthetic-account-9031"
    return value


def main() -> None:
    settings = load_settings()
    authorization_case = historical(concentration_authorization_payload(settings))
    authorization_case["input"]["account"]["account_id"] = "synthetic-account-8029"
    command = authorization_case["portfolio"]
    portfolio_id = "synthetic-concentration-browser-portfolio"
    command["proposal"]["portfolio_id"] = portfolio_id
    command["confirmation"]["portfolio_id"] = portfolio_id
    authorized = run_frozen_decision_case(settings, authorization_case)
    assert authorized.report is not None
    portfolio = authorized.report.result.portfolio
    assert portfolio is not None and portfolio.authorization is not None, portfolio
    case = historical(
        concentration_payload(settings, portfolio.authorization.authorization_id, quantity="100")
    )
    case["concentration"]["authorization"]["portfolio_id"] = portfolio_id
    case["input"]["account"]["account_id"] = "synthetic-account-8029"
    Path(sys.argv[1]).write_text(json.dumps(case), encoding="utf-8")


if __name__ == "__main__":
    main()
