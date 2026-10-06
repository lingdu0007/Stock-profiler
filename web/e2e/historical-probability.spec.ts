import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import type { FormalReport } from "../src/api/client";

const { report } = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/synthetic/historical_probability.json", import.meta.url),
    "utf8"
  )
) as { report: FormalReport };

for (const width of [1280, 375]) {
  test(`reads saved probability gates and population evidence at width ${width}`, async ({
    page
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/v1/reports/**", (route) =>
      route.fulfill({ status: 200, json: report })
    );
    await page.goto(`/reports/${report.report_version_id}`);
    const evidence = page.getByRole("region", {
      name: "Historical probability evidence",
      exact: true
    });
    await expect(evidence).toContainText("INSUFFICIENT");
    await expect(evidence).toContainText("overall:overconfidence");
    await expect(evidence).toContainText("Log Loss");
    await expect(evidence).toContainText("BULL");
    await expect(evidence).toContainText("No recommendation or execution authorization");
    await expect(evidence.getByRole("button")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width
    );
  });
}
