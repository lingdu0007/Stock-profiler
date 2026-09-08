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

it.each([
  ["ZERO_DEPLOYABLE_CASH", "Zero deployable cash"],
  ["REMEDIATION_REQUIRED", "Liquidity restoration required"],
  ["FUNDING_INFEASIBLE", "Funding infeasible"],
  ["EVIDENCE_FAILED", "Liquidity evidence failed"]
])(
  "renders committed liquidity state %s without computing money in the browser",
  async (state, label) => {
    const report = {
      ...syntheticReport,
      result: {
        ...syntheticReport.result,
        liquidity: {
          disposition: state,
          reasons: ["SYNTHETIC_LIQUIDITY_REASON"],
          authorization_id: "synthetic-liquidity-authorization",
          qualified_cash: state === "EVIDENCE_FAILED" ? null : "95",
          normal_cash_target: "510.90",
          hard_cash_floor: "301.30",
          net_liquidation_equity: "1310",
          deployable_purchase_cash: state === "EVIDENCE_FAILED" ? null : "0",
          remediation_id: "synthetic-restoration-event",
          remediation_shortfall: null,
          retained_remediation_shortfall: "415.90",
          maximum_fundable_cash: "930",
          uncovered_obligation_gap: "380",
          new_exposure_blocked: true,
          maximum_funding_plan: [],
          obligation_funding: [
            {
              obligation_id: "synthetic-dated-obligation",
              target_account_id: "synthetic-target-account",
              latest_usable_at: "2042-05-20T16:00:00Z",
              required_cash: "1310",
              maximum_covered_cash: "930",
              uncovered_gap: "380"
            }
          ],
          settled_coverage: []
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
    const section = await screen.findByRole("region", { name: "Liquidity protection" });
    expect(section).toHaveTextContent(label);
    expect(section).toHaveTextContent("synthetic-restoration-event");
    expect(section).toHaveTextContent("415.90");
    expect(section).toHaveTextContent("synthetic-target-account");
    expect(section).toHaveTextContent("380");
    expect(section).toHaveTextContent("Blocked");
    if (state === "EVIDENCE_FAILED") {
      expect(section).toHaveTextContent("Unknown");
    }
  }
);
