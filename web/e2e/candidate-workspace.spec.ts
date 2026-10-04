import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import type { CandidateWorkspace } from "../src/api/client";

const { workspace } = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/synthetic/candidate_workspace.json", import.meta.url),
    "utf8"
  )
) as { workspace: CandidateWorkspace };

for (const width of [1280, 375, 320]) {
  test(`candidate release, detail and original history at width ${width}`, async ({
    page
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const history = structuredClone(workspace);
    history.releases[0].status = "SUPERSEDED";
    history.releases[0].status_reasons = ["EVIDENCE_INVALIDATED"];
    history.releases[0].superseded_by_report_id = "synthetic-candidate-replacement";
    history.releases.push({
      ...workspace.releases[0],
      report_version_id: "synthetic-candidate-replacement",
      event_id: "synthetic-candidate-replacement-event",
      corrects_report_id: "synthetic-candidate-report",
      detail_ids: [],
      status: "INVALIDATED",
      status_reasons: ["CURRENT_QUALIFICATION_REVOKED"]
    });
    history.current_report_ids = ["synthetic-candidate-replacement"];
    const methods: string[] = [];
    await page.route("**/api/v1/candidates", (route) => {
      methods.push(route.request().method());
      return route.fulfill({ json: history });
    });
    await page.goto("/candidates");
    await page.getByRole("link", { name: /2042-07.*INVALIDATED/ }).click();
    await expect(page.getByText(/CURRENT_QUALIFICATION_REVOKED/)).toBeVisible();
    await page.getByRole("link", { name: "Read original release" }).click();
    await expect(page).toHaveURL(/synthetic-candidate-report$/);
    await expect(page.getByText(/Historical result/)).toBeVisible();
    await page.getByRole("link", { name: "SYNTH-CANDIDATE", exact: true }).click();
    await expect(page).toHaveURL(/synthetic-candidate-detail$/);
    await expect(page.getByText("Invented synthetic research thesis.")).toBeVisible();
    await expect(page.getByText(/2042-07-07, 23:00:00 Asia\/Shanghai/)).toBeVisible();
    await expect(page.getByRole("button", { name: /confirm|accept|execute|reserve/i })).toHaveCount(
      0
    );
    expect(methods.every((method) => method === "GET")).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width
    );
    await page.screenshot({
      path: info.outputPath(`candidate-history-detail-${width}.png`),
      fullPage: true
    });
    await page.getByRole("link", { name: "Candidate history", exact: true }).click();
    await expect(page.getByRole("link", { name: /2042-07.*SUPERSEDED/ })).toBeVisible();
  });
}
