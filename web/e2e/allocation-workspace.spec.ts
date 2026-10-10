import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import type { CandidateWorkspace, CandidateWorkspaceCommand } from "../src/api/client";

const fixture = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/synthetic/allocation_workspace.json", import.meta.url),
    "utf8"
  )
) as { workspace: CandidateWorkspace };

for (const width of [1280, 375, 320]) {
  test(`saved allocation, complete intent, policy and immutable report at width ${width}`, async ({
    page
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.clock.install({ time: new Date("2042-05-19T16:01:00Z") });
    const workspace = structuredClone(fixture.workspace);
    const view = workspace.allocations[0];
    const commands: CandidateWorkspaceCommand[] = [];
    await page.route("**/api/v1/candidates", (route) => route.fulfill({ json: workspace }));
    await page.route("**/api/v1/candidates/commands", async (route) => {
      const request = route.request().postDataJSON() as CandidateWorkspaceCommand;
      commands.push(request);
      const report = structuredClone(view.plan_report);
      report.report_version_id = "synthetic-workspace-review-report";
      report.event_id = "synthetic-workspace-review-event";
      report.result.candidate_allocation = null;
      report.result.candidate_confirmation = {
        disposition: "REVALIDATED",
        reasons: [],
        choices: [],
        reservations: [],
        released_reservation_ids: [],
        actionable: false
      };
      await route.fulfill({ json: { report } });
    });
    await page.route("**/api/v1/reports/*", (route) => route.fulfill({ json: view.plan_report }));
    await page.goto("/candidates");
    await expect(
      page.getByRole("heading", { name: "Personal allocation and execution" })
    ).toBeVisible();
    await expect(page.getByRole("table", { name: "Saved capacity margins" })).toBeVisible();
    await page
      .getByRole("combobox", { name: /Choice for/ })
      .first()
      .selectOption("ACCEPT");
    expect(commands).toHaveLength(0);
    await expect(page.getByRole("button", { name: "Confirm complete batch" })).toBeDisabled();
    await page.getByRole("button", { name: "Replay current policy" }).click();
    await expect(page.getByText(/Policy replay: REVALIDATED/)).toBeVisible();
    await page.getByRole("button", { name: "Confirm complete batch" }).click();
    await expect.poll(() => commands.length).toBe(2);
    expect(commands[1].plan_report_version_id).toBe(view.report_version_id);
    expect(commands[1].choices).toHaveLength(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width
    );
    await page.getByRole("link", { name: "Read original allocation report" }).click();
    await expect(page).toHaveURL(new RegExp(`/reports/${view.report_version_id}$`));
    await expect(page.getByRole("heading", { name: "Candidate allocation" })).toBeVisible();
  });
}
