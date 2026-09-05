import { expect, test } from "@playwright/test";

const report = {
  report_version_id: "report-version-4017",
  event_id: "decision-event-4017",
  business_object_id: "business-object-4017",
  framework_run_id: "framework-run-4017",
  case_id: "d0-orbital-mosaic-001",
  synthetic: true,
  qualification_scope: "D0_SYNTHETIC_CONTRACT_ONLY",
  generated_at: "2042-05-17T15:18:00Z",
  knowledge_cutoff: "2042-05-17T16:00:00Z",
  evidence_clock: {
    fact_effective_at: "2042-05-17T15:00:00Z",
    source_published_at: "2042-05-17T15:12:00Z",
    acquired_at: "2042-05-17T15:16:00Z",
    validated_at: "2042-05-17T15:18:00Z"
  },
  version_bundle: {
    case_contract_version: "1.0.0",
    host_contract_version: "1.0.0",
    agent_definition_id: "synthetic-frozen-decision-case",
    agent_definition_version: "1.0.0",
    output_contract_version: "1.0.0",
    m_agent_version: "0.5.0",
    m_agent_release_commit: "743651e5c74a4865f25a31dab68d188b5b0aed64"
  },
  result: {
    outcome_code: "SYNTHETIC_REVIEW_COMPLETE",
    summary: "Synthetic D0 decision case completed under the frozen contract.",
    key_reasons: [
      "All required fictional evidence records are present.",
      "The output is D0 synthetic evidence and is not a recommendation."
    ]
  }
};

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
