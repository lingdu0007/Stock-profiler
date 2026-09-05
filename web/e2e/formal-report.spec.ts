import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

import type { FormalReport } from "../src/api/client";

type SyntheticReportFixture = {
  synthetic: boolean;
  generator_version: string;
  seed: number;
  report: FormalReport;
};

const reportFixture = JSON.parse(
  await readFile(
    new URL("../../tests/fixtures/synthetic/formal_report_projection.json", import.meta.url),
    "utf8"
  )
) as SyntheticReportFixture;
const report = reportFixture.report;

test("shows a D0 report returned by the same API projection on every page load", async ({
  page
}) => {
  let reportRequests = 0;
  await page.route(`**/api/v1/reports/${report.report_version_id}`, async (route) => {
    reportRequests += 1;
    await route.fulfill({
      contentType: "application/json",
      headers: { "Cache-Control": "no-store" },
      body: JSON.stringify(report)
    });
  });

  await page.goto(`/reports/${report.report_version_id}`);
  await expect(page.getByText("SYNTHETIC_REVIEW_COMPLETE")).toBeVisible();
  await expect(page.getByText("decision-event-4017")).toBeVisible();
  await expect(page.getByText("framework-run-4017")).toBeVisible();
  await expect(page.getByText("D0 synthetic", { exact: true })).toBeVisible();

  await page.reload();
  await expect(page.getByText("SYNTHETIC_REVIEW_COMPLETE")).toBeVisible();
  expect(reportRequests).toBe(2);
});

test("does not reveal a report when the API returns an unauthenticated response", async ({
  page
}) => {
  await page.route(`**/api/v1/reports/${report.report_version_id}`, async (route) => {
    await route.fulfill({ status: 401 });
  });

  await page.goto(`/reports/${report.report_version_id}`);

  await expect(page.getByRole("button", { name: "Continue with Passkey" })).toBeVisible();
  await expect(page.getByText("SYNTHETIC_REVIEW_COMPLETE")).not.toBeVisible();
});
