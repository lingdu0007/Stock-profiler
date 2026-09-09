import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

import { selectionReport } from "../src/test-support/selection-report";
import type { FormalReport } from "../src/api/client";

const { report: original } = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/synthetic/formal_report_projection.json", import.meta.url),
    "utf8"
  )
) as { report: FormalReport };

for (const width of [1280, 375]) {
  test(`retains ordered selection and scan at width ${width}`, async ({ page }, info) => {
    const report = selectionReport(original, "FROZEN");
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/v1/reports/**", async (route) => {
      await route.fulfill({ status: 200, json: report });
    });
    await page.goto(`/reports/${report.report_version_id}`);
    const selection = page.getByRole("region", { name: "Monthly selection" });
    await expect(selection).toContainText("Non-actionable");
    await expect(selection).toContainText("Research priority");
    await expect(selection.getByRole("list").first()).toHaveCSS("list-style-type", "decimal");
    await expect(selection.getByRole("list").first()).toHaveCSS("list-style-position", "inside");
    await selection.getByText("Full research ranking (12)").click();
    await expect(selection.getByText("XQZ-SELECT-11: 51", { exact: true })).toBeVisible();
    await selection.getByText("Diversification scan (1)").click();
    await expect(selection).toContainText("Included during scan");
    await selection.getByText("Screening contributions (1)").click();
    await expect(selection.getByText("1.25; OBSERVED; 75")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width
    );
    await selection.screenshot({ path: info.outputPath(`selection-${width}.png`) });
  });
}
