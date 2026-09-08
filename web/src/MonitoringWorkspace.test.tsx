import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { App } from "./App";
import { syntheticReport } from "./test-support/synthetic-report";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

it("keeps current obligations visible across overview, inbox and immutable archive", async () => {
  const item = {
    case_id: "synthetic-monitoring-case",
    source_event_id: "synthetic-plan-event",
    obligation_ids: ["synthetic-obligation"],
    priority: "P1",
    plan: null,
    quantity_status: "UNKNOWN",
    first_established_at: "2042-05-17T16:00:00Z",
    last_reviewed_at: "2042-05-17T16:00:00Z"
  };
  const report = {
    ...syntheticReport,
    result: {
      ...syntheticReport.result,
      monitoring: {
        portfolio_id: "synthetic-portfolio",
        kind: "DAILY_CLOSE",
        disposition: "BLOCKED",
        reasons: ["MONITORING_EVIDENCE_INCOMPLETE"],
        cases: [item],
        action_units: [],
        freshness: null,
        notifications: [],
        source_report_ids: [],
        reconciliation: null
      }
    }
  };
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            reports: [report],
            current_report_ids: [report.report_version_id],
            inbox: [item],
            user_facts: []
          }),
          { headers: { "Content-Type": "application/json" } }
        )
      )
    )
  );
  window.history.pushState({}, "", "/monitoring");
  render(<App />);
  expect(await screen.findByText("MONITORING_EVIDENCE_INCOMPLETE")).toBeVisible();
  expect(screen.getByText("Quantity unknown")).toBeVisible();
  expect(screen.getByRole("button", { name: "Acknowledge risk" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Record execution declaration" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Record plan choice" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: "Inbox" }));
  expect(await screen.findByText("Deterministic obligation persists")).toBeVisible();
  fireEvent.click(screen.getByRole("link", { name: "Archive" }));
  expect(await screen.findByRole("link", { name: report.report_version_id })).toBeVisible();
  expect(screen.queryByRole("button", { name: /close obligation/i })).not.toBeInTheDocument();
});
