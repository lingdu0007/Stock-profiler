import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

import type { FormalReport, MonitoringWorkspace } from "../src/api/client";

const { report: baseline } = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/synthetic/formal_report_projection.json", import.meta.url),
    "utf8"
  )
) as { report: FormalReport };

const report: FormalReport = {
  ...baseline,
  result: {
    ...baseline.result,
    monitoring: {
      portfolio_id: "synthetic-monitoring-portfolio",
      kind: "DAILY_CLOSE",
      disposition: "BLOCKED",
      reasons: ["MONITORING_EVIDENCE_INCOMPLETE"],
      cases: [
        {
          case_id: "synthetic-persistent-case",
          source_event_id: "synthetic-saved-plan",
          obligation_ids: ["synthetic-risk-obligation"],
          priority: "P1",
          plan: null,
          quantity_status: "UNKNOWN",
          first_established_at: "2042-05-17T16:00:00Z",
          last_reviewed_at: "2042-05-17T16:00:00Z"
        }
      ],
      action_units: [],
      notifications: [],
      source_report_ids: []
    }
  }
};
const workspace: MonitoringWorkspace = {
  reports: [report],
  current_report_ids: [report.report_version_id],
  inbox: report.result.monitoring!.cases,
  user_facts: []
};

for (const width of [1280, 375]) {
  test(`monitoring navigation and independent declaration at width ${width}`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/v1/monitoring", (route) => route.fulfill({ json: workspace }));
    await page.route("**/api/v1/reports/*/facts", async (route) => {
      const input = route.request().postDataJSON() as { kind: string; declaration: string };
      expect(input.kind).toBe("EXECUTION_DECLARED");
      await route.fulfill({
        json: {
          fact_id: "synthetic-declaration",
          report_version_id: report.report_version_id,
          event_id: report.event_id,
          kind: input.kind,
          choice: null,
          declaration: input.declaration,
          reconciliation_status: "PENDING",
          recorded_at: "2042-05-18T16:00:00Z",
          synthetic: true,
          authoritative_execution: false
        }
      });
    });
    await page.goto("/monitoring");
    await expect(page.getByText("Quantity unknown")).toBeVisible();
    await page
      .getByRole("combobox", { name: "Execution declaration", exact: true })
      .selectOption("REPORTED_FILLED");
    await page.getByRole("button", { name: "Record execution declaration" }).click();
    await expect(page.getByRole("status")).toContainText("Authoritative reconciliation pending");
    await expect(page.getByText("Deterministic obligation persists")).toBeVisible();
    await page.screenshot({
      path: info.outputPath(`monitoring-overview-${width}.png`),
      fullPage: true
    });
    await page.getByRole("link", { name: "Inbox" }).click();
    await expect(page.getByText("synthetic-persistent-case")).toBeVisible();
    await page.getByRole("link", { name: "Archive" }).click();
    await expect(page.getByRole("link", { name: report.report_version_id })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width
    );
    await page.screenshot({
      path: info.outputPath(`monitoring-archive-${width}.png`),
      fullPage: true
    });
  });
}
