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

it.each([true, false])("records complete report views with obligations=%s", async (hasCases) => {
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
        cases: hasCases ? [item] : [],
        action_units: [],
        freshness: null,
        notifications: [],
        source_report_ids: [],
        reconciliation: null
      }
    }
  };
  const recorded: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      if (request.method === "POST") {
        const fact = (await request.json()) as Record<string, unknown>;
        recorded.push(fact);
        return new Response(
          JSON.stringify({
            ...fact,
            fact_id: "synthetic-view",
            report_version_id: report.report_version_id,
            event_id: report.event_id,
            recorded_at: "2042-05-17T16:01:00Z",
            reconciliation_status: "NOT_APPLICABLE",
            authoritative_execution: false,
            synthetic: true
          }),
          { headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response(
        JSON.stringify(
          request.url.includes("/api/v1/reports/")
            ? report
            : {
                reports: [report],
                current_report_ids: [report.report_version_id],
                inbox: hasCases ? [item] : [],
                user_facts: []
              }
        ),
        { headers: { "Content-Type": "application/json" } }
      );
    })
  );
  window.history.pushState({}, "", "/monitoring");
  render(<App />);
  expect(await screen.findByText("MONITORING_EVIDENCE_INCOMPLETE")).toBeVisible();
  if (hasCases) {
    expect(screen.getByText("Quantity unknown")).toBeVisible();
    expect(screen.getByRole("button", { name: "Acknowledge risk" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Record execution declaration" })).toBeVisible();
  } else {
    expect(screen.queryByRole("button", { name: "Acknowledge risk" })).not.toBeInTheDocument();
  }
  expect(screen.queryByRole("button", { name: "Record plan choice" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: "Inbox" }));
  if (hasCases) {
    expect(await screen.findByText("Deterministic obligation persists")).toBeVisible();
  }
  fireEvent.click(screen.getByRole("link", { name: "Archive" }));
  expect(await screen.findByRole("link", { name: report.report_version_id })).toBeVisible();
  expect(screen.queryByRole("button", { name: /close obligation/i })).not.toBeInTheDocument();
  expect(recorded).toEqual([]);
  fireEvent.click(screen.getByRole("link", { name: report.report_version_id }));
  expect(await screen.findByRole("status")).toHaveTextContent("VIEWED recorded");
  expect(recorded).toEqual([expect.objectContaining({ kind: "VIEWED" })]);
});
