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

it.each(["ACCEPTED", "UNKNOWN"])(
  "renders retained %s capital facts without recomputing them",
  async (disposition) => {
    const report = {
      ...syntheticReport,
      result: {
        ...syntheticReport.result,
        drawdown: {
          disposition,
          reasons: ["SYNTHETIC_CAPITAL_EVIDENCE"],
          state: {
            epoch_id: "synthetic-capital-epoch-alpha",
            authorization_id: "synthetic-capital-authorization",
            cutoff_at: "2042-05-21T16:00:00Z",
            epoch_status: "OPEN",
            risk_state: "DEFENSIVE",
            unit_nav: disposition === "UNKNOWN" ? null : "0.84",
            units: "1300",
            high_water_nav: "1",
            current_drawdown: disposition === "UNKNOWN" ? null : "0.16",
            maximum_drawdown: "0.18",
            net_liquidation_equity: disposition === "UNKNOWN" ? null : "1092",
            current_stock_exposure: disposition === "UNKNOWN" ? null : "992",
            stock_exposure_limit: "0.27",
            stock_exposure_target_value: disposition === "UNKNOWN" ? null : "294.84",
            new_exposure_blocked: true,
            risk_direction: "REDUCE",
            execution_blocked: true,
            recovery_sessions: 0,
            cooling_sessions: 0,
            previous_epoch_id: null,
            policy: { version_id: "synthetic-drawdown-policy-alpha" },
            valuation: { position_event_id: "synthetic-position-anchor" }
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
    const region = await screen.findByRole("region", { name: "Capital protection" });
    for (const value of [
      disposition,
      "DEFENSIVE",
      "synthetic-capital-epoch-alpha",
      "0.18",
      "REDUCE",
      "synthetic-position-anchor",
      "Blocked"
    ]) {
      expect(region).toHaveTextContent(value);
    }
    expect(region).toHaveTextContent(disposition === "UNKNOWN" ? "Unknown" : "294.84");
  }
);
