import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

import type { FormalReport } from "../src/api/client";

const { report: syntheticReport } = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/synthetic/formal_report_projection.json", import.meta.url),
    "utf8"
  )
) as { report: FormalReport };

const report: FormalReport = {
  ...syntheticReport,
  result: {
    ...syntheticReport.result,
    execution_plan: {
      disposition: "RECONFIRMATION_REQUIRED",
      reasons: ["TRADING_UNIT_FULL_SALE"],
      new_exposure_blocked: true,
      risk_restored: false,
      requires_confirmation: true,
      legs: [],
      targets: [
        {
          security_id: "XQZ-4017",
          target_quantity: "60",
          required_sale_quantity: "140",
          remaining_quantity: "0",
          remaining_gap: "0",
          source_obligation_ids: ["synthetic-obligation"],
          direction: "REDUCE",
          rounding_induced_full_sale: true
        }
      ]
    }
  }
};

for (const width of [1280, 375]) {
  test(`retains full-sale reconfirmation and original target at width ${width}`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/v1/reports/**", async (route) => {
      await route.fulfill({ status: 200, json: report });
    });
    await page.goto(`/reports/${report.report_version_id}`);
    const plan = page.getByRole("region", { name: "Execution plan" });
    await expect(plan).toContainText("Trading-unit full sale: reconfirmation required");
    await expect(plan).toContainText("REDUCE");
    await expect(plan).toContainText("60");
    await expect(plan).toContainText("Not restored by this plan");
    await expect(plan).toContainText("synthetic-obligation");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width
    );
    await plan.screenshot({ path: info.outputPath(`execution-${width}.png`) });
  });
}
