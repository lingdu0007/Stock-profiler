import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "./App";
import { selectionReport } from "./test-support/selection-report";
import { syntheticReport } from "./test-support/synthetic-report";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

it.each(["FROZEN", "ABSTAINED", "DATA_FAILED"] as const)(
  "shows the committed selection %s with separate population status",
  async (disposition) => {
    const report = selectionReport(syntheticReport, disposition);
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
    const section = await screen.findByRole("region", { name: "Monthly selection" });
    expect(section).toHaveTextContent(disposition);
    expect(section).toHaveTextContent("Research priority");
    expect(section).toHaveTextContent("Non-actionable");
    expect(section).toHaveTextContent("synthetic-universe-event");
    expect(section).toHaveTextContent("Recommendation coverage denominator");
    expect(section).toHaveTextContent("2042-05-30T23:59:59+08:00");
    if (disposition !== "FROZEN") expect(section).toHaveTextContent("No frozen members");
    if (disposition !== "DATA_FAILED") {
      expect(section).toHaveTextContent("Screening contributions (1)");
      expect(section).toHaveTextContent("synthetic-cash-signal");
      expect(section).toHaveTextContent("1.25");
    }
  }
);
