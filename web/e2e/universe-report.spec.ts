import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

import type { FormalReport } from "../src/api/client";

const { report: original } = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/synthetic/formal_report_projection.json", import.meta.url),
    "utf8"
  )
) as { report: FormalReport };

const report: FormalReport = {
  ...original,
  result: {
    ...original.result,
    universe: {
      disposition: "FROZEN",
      cutoff_at: "2042-05-30T23:59:59+08:00",
      policy: {
        version_id: "synthetic-universe-policy-v1",
        minimum_listing_months: 6,
        turnover_sessions: 4,
        minimum_median_turnover: "20000",
        maximum_participation: "0.02"
      },
      qualification_scope: "D0_SYNTHETIC_CONTRACT_ONLY",
      actionable: false,
      members: ["XQZ-UNIVERSE-731"],
      exclusions: [{ security_id: "XQZ-UNIVERSE-EXCLUDED", reasons: ["RISK_WARNING"] }],
      reasons: [],
      board_qualifications: [],
      manifest: {
        version_id: "synthetic-universe-manifest-v1",
        entries: [
          {
            field_family: "INVENTORY",
            requirement: "REQUIRED",
            semantics_version: "synthetic-security-semantics-v1",
            primary_source: "fictional-exchange-731",
            evidence: {
              evidence_id: "synthetic-inventory-731",
              source: "fictional-exchange-731",
              source_version: "synthetic-source-v1",
              authority: "EXCHANGE",
              license_id: "synthetic-license-v1",
              license_valid_from: "2042-01-01T00:00:00+08:00",
              license_valid_until: "2042-12-31T23:59:59+08:00",
              licensed_purposes: ["SYNTHETIC"],
              retention_permitted: true,
              complete: true,
              conflict: false,
              fact_effective_at: "2042-05-30T15:00:00+08:00",
              source_published_at: "2042-05-30T15:01:00+08:00",
              source_observed_at: "2042-05-30T15:02:00+08:00",
              acquired_at: "2042-05-30T15:03:00+08:00",
              validated_at: "2042-05-30T15:04:00+08:00",
              complete_through: "2042-05-30T23:59:59+08:00",
              content: '["XQZ-UNIVERSE-731"]',
              content_sha256: "a".repeat(64)
            }
          }
        ]
      }
    }
  }
};

for (const width of [1280, 375]) {
  test(`retains frozen universe evidence at width ${width}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/v1/reports/**", async (route) => {
      await route.fulfill({ status: 200, json: report });
    });
    await page.goto(`/reports/${report.report_version_id}`);
    const universe = page.getByRole("region", { name: "Monthly universe" });
    await expect(universe).toContainText("Non-actionable");
    await expect(universe).toContainText("XQZ-UNIVERSE-731");
    await expect(universe).toContainText("RISK_WARNING");
    await universe.getByText("INVENTORY: REQUIRED").click();
    await expect(universe).toContainText("synthetic-license-v1");
    await expect(universe).toContainText("2042-12-31T23:59:59+08:00");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width
    );
    await universe.screenshot({ path: info.outputPath(`universe-${width}.png`) });
  });
}
