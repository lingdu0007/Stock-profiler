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

it("preserves the reduction reason while showing a rounding-induced full sale", async () => {
  const report = {
    ...syntheticReport,
    result: {
      ...syntheticReport.result,
      execution_plan: {
        disposition: "RECONFIRMATION_REQUIRED",
        reasons: ["TRADING_UNIT_FULL_SALE"],
        new_exposure_blocked: true,
        risk_restored: false,
        requires_confirmation: true,
        initial_target_sale_value: "2000",
        projected_stress_gap: "0",
        projected_cash_gap: "0",
        projected_exposure_gap: "0",
        cost_routing_basis: "VERIFIED_FULL_COST",
        legs: [],
        targets: [
          {
            security_id: "XQZ-4017",
            target_quantity: "60",
            required_sale_quantity: "140",
            remaining_quantity: "0",
            remaining_gap: "0",
            source_obligation_ids: ["synthetic-obligation"],
            direction: "REDUCE",
            rounding_induced_full_sale: true
          }
        ]
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
  const section = await screen.findByRole("region", { name: "Execution plan" });
  expect(section).toHaveTextContent("Reconfirmation required");
  expect(section).toHaveTextContent("Trading-unit full sale");
  expect(section).toHaveTextContent("REDUCE");
  expect(section).toHaveTextContent("60");
  expect(section).toHaveTextContent("Not restored by this plan");
  expect(section).toHaveTextContent("synthetic-obligation");
});
