import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import type { FormalReport } from "../src/api/client";

const { report } = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/synthetic/standard_outcomes.json", import.meta.url),
    "utf8"
  )
) as { report: FormalReport };

for (const width of [1280, 375]) {
  test(`reads saved standard outcomes and missing denominators at width ${width}`, async ({
    page
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/v1/reports/**", (route) =>
      route.fulfill({ status: 200, json: report })
    );
    await page.goto(`/reports/${report.report_version_id}`);
    const outcomes = page.getByRole("region", { name: "Standard outcomes", exact: true });
    await expect(outcomes).toContainText("INDETERMINATE");
    await expect(outcomes).toContainText("Due outcomes missing");
    await expect(outcomes).toContainText("20.00%");
    await expect(outcomes).toContainText("Expired");
    await expect(
      outcomes.getByRole("link", { name: "Read saved candidate report" })
    ).toHaveAttribute("href", /\/reports\//);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width
    );
  });
}
