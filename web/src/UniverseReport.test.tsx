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

it("shows the saved universe and exclusions without turning membership into an action", async () => {
  const report = {
    ...syntheticReport,
    result: {
      ...syntheticReport.result,
      universe: {
        disposition: "FROZEN",
        cutoff_at: "2042-05-30T23:59:59+08:00",
        policy: { version_id: "synthetic-universe-policy-v1" },
        qualification_scope: "D0_SYNTHETIC_CONTRACT_ONLY",
        actionable: false,
        members: ["XQZ-UNIVERSE-731"],
        exclusions: [{ security_id: "XQZ-UNIVERSE-EXCLUDED", reasons: ["RISK_WARNING"] }],
        reasons: [],
        board_qualifications: [],
        manifest: { version_id: "synthetic-universe-manifest-v1", entries: [] }
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
  const section = await screen.findByRole("region", { name: "Monthly universe" });
  expect(section).toHaveTextContent("XQZ-UNIVERSE-731");
  expect(section).toHaveTextContent("XQZ-UNIVERSE-EXCLUDED");
  expect(section).toHaveTextContent("RISK_WARNING");
  expect(section).toHaveTextContent("Non-actionable");
  expect(section).toHaveTextContent("2042-05-30T23:59:59+08:00");
  expect(section).toHaveTextContent("synthetic-universe-manifest-v1");
});
