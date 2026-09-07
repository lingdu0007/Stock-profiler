import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "./App";
import { syntheticReport } from "./test-support/synthetic-report";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

it("renders a retained stress obligation without replacing unknown values with zero", async () => {
  const report = {
    ...syntheticReport,
    result: {
      ...syntheticReport.result,
      stress: {
        state: "UNKNOWN",
        reasons: ["STRESS_VALUATION_UNKNOWN"],
        portfolio_id: "synthetic-decision-portfolio-alpha",
        authorization_id: "synthetic-stress-authorization",
        snapshot_id: "synthetic-stress-snapshot",
        cutoff_at: "2042-05-19T16:00:00Z",
        account_ids: ["synthetic-account-4017"],
        gross_stress_loss: null,
        net_liquidation_equity: null,
        stress_ratio: null,
        contributions: [],
        new_exposure_blocked: true,
        execution_blocked: true,
        risk_budget_version_id: "synthetic-risk-budget-alpha",
        calculation_policy: {
          contract_version: "1.0.0",
          version_id: "synthetic-gross-stress-v1",
          horizon_market_days: 20,
          shock_ratio: "0.23",
          disposal_friction_ratio: "0.017",
          registered_at: "2042-05-16T16:00:00Z"
        },
        budget: { target_ratio: "0.14", hard_ratio: "0.18" },
        residual_restoration_gap: null,
        obligation: {
          obligation_id: "synthetic-stress-obligation",
          direction: "REDUCE_TOTAL_STOCK_EXPOSURE",
          target_stress_ratio: "0.14",
          status: "OUTSTANDING",
          triggered_at: "2042-05-17T16:00:00Z"
        }
      }
    }
  };
  window.history.pushState({}, "", `/reports/${report.report_version_id}`);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(report), {
        status: 200,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
      })
    )
  );
  render(<App />);
  const region = await screen.findByRole("region", { name: "Portfolio gross stress" });
  expect(region).toHaveTextContent("STRESS_VALUATION_UNKNOWN");
  expect(region).toHaveTextContent("REDUCE_TOTAL_STOCK_EXPOSURE");
  expect(region).toHaveTextContent("OUTSTANDING");
  expect(region).toHaveTextContent("synthetic-stress-obligation");
  expect(region).toHaveTextContent("2042-05-17T16:00:00Z");
  expect(region).toHaveTextContent("Unknown");
  expect(region).toHaveTextContent("Blocked");
  expect(region).toHaveTextContent("synthetic-gross-stress-v1");
  expect(region).toHaveTextContent("synthetic-risk-budget-alpha");
  expect(region).toHaveTextContent("0.23");
  expect(region).toHaveTextContent("0.017");
  expect(region).toHaveTextContent("0.18");
  expect(region).toHaveTextContent("2042-05-16T16:00:00Z");
});
