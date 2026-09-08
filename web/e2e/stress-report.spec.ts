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
    stress: {
      state: "HARD_BREACH",
      reasons: [],
      portfolio_id: "synthetic-decision-portfolio-alpha",
      authorization_id: "synthetic-stress-authorization",
      snapshot_id: "synthetic-stress-snapshot",
      cutoff_at: "2042-05-17T16:00:00Z",
      account_ids: ["synthetic-account-4017"],
      gross_stress_loss: "296.400",
      net_liquidation_equity: "1646.666",
      stress_ratio: "0.180000072874523431",
      contributions: [
        {
          account_id: "synthetic-account-4017",
          security_id: "XQZ-4017",
          current_exposure: "1200",
          adverse_price_loss: "276.00",
          disposal_friction: "20.400"
        }
      ],
      new_exposure_blocked: true,
      execution_blocked: true,
      residual_restoration_gap: "62.90276",
      risk_budget_version_id: "synthetic-risk-budget-alpha",
      calculation_policy: {
        contract_version: "1.0.0",
        version_id: "synthetic-gross-stress-v1",
        horizon_market_days: 20,
        shock_ratio: "0.23",
        disposal_friction_ratio: "0.017",
        registered_at: "2042-05-16T16:00:00Z"
      },
      budget: { target_ratio: "0.14", hard_ratio: "0.18" },
      obligation: {
        obligation_id: "synthetic-stress-obligation",
        direction: "REDUCE_TOTAL_STOCK_EXPOSURE",
        target_stress_ratio: "0.14",
        status: "OUTSTANDING",
        triggered_at: "2042-05-17T16:00:00Z"
      }
    }
  }
};

for (const width of [1280, 375]) {
  test(`retains stress policy and restoration evidence at width ${width}`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/v1/reports/**", async (route) => {
      await route.fulfill({ status: 200, json: report });
    });
    await page.goto(`/reports/${report.report_version_id}`);
    const stress = page.getByRole("region", { name: "Portfolio gross stress" });
    await expect(stress).toContainText("HARD_BREACH");
    await expect(stress).toContainText("synthetic-gross-stress-v1");
    await expect(stress).toContainText("62.90276");
    await expect(stress).toContainText("OUTSTANDING");
    await expect(stress).toContainText("XQZ-4017");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width
    );
    await stress.screenshot({ path: info.outputPath(`stress-${width}.png`) });
  });
}
