import { expect, test } from "@playwright/test";
import { execFile as executeFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import type { FormalReport } from "../src/api/client";

type DecisionCaseExecution = {
  report: FormalReport;
};

const execFile = promisify(executeFile);
const repositoryRoot = fileURLToPath(new URL("../..", import.meta.url));
let temporaryDirectory = "";
let report: FormalReport;

test.beforeAll(async () => {
  temporaryDirectory = await mkdtemp(join(tmpdir(), "stock-profiler-playwright-"));
  const applicationDatabase = join(temporaryDirectory, "application.sqlite3");
  const environment = {
    ...process.env,
    STOCK_PROFILER_APP_DATABASE_URL: `sqlite:///${applicationDatabase}`,
    STOCK_PROFILER_ENVIRONMENT: "test",
    STOCK_PROFILER_M_AGENT_RUN_STORE_PATH: join(temporaryDirectory, "m-agent-runs.sqlite3"),
    STOCK_PROFILER_SOURCE_SHA: "a".repeat(40)
  };
  await execFile("uv", ["run", "alembic", "upgrade", "head"], {
    cwd: repositoryRoot,
    env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "migrate" }
  });
  const execution = await execFile("uv", ["run", "stock-profiler", "decision-case-run"], {
    cwd: repositoryRoot,
    env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "cli" }
  });
  report = (JSON.parse(execution.stdout) as DecisionCaseExecution).report;
});

test.afterAll(async () => {
  if (temporaryDirectory) {
    await rm(temporaryDirectory, { force: true, recursive: true });
  }
});

test("shows the committed CLI report projection on every page load", async ({ page }) => {
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
  await expect(page.getByText(report.event_id)).toBeVisible();
  await expect(page.getByText(report.framework_run_id)).toBeVisible();
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
