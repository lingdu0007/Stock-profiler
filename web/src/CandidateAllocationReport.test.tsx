import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import workspace from "../../tests/fixtures/synthetic/candidate_workspace.json";
import { App } from "./App";
import { syntheticReport } from "./test-support/synthetic-report";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

it("shows a saved partial allocation without replacing qualification or probability", async () => {
  const member = workspace.workspace.releases[0].release.members[0];
  const report = {
    ...syntheticReport,
    result: {
      ...syntheticReport.result,
      candidate_allocation: {
        disposition: "PLANNED",
        reasons: [],
        actionable: false,
        plan_id: "synthetic-allocation",
        formed_at: "2042-05-17T16:00:00Z",
        candidate_batch_id: "synthetic-batch",
        candidate_conclusion_version: "synthetic-source-report",
        position_snapshot_id: "synthetic-complete-portfolio",
        risk_budget_version_id: "synthetic-budget",
        total_principal: "200",
        remaining_cash: "3890",
        purchase_sequence: [member.security_id],
        rows: [
          {
            candidate: member,
            issuer_id: "fictional-new-issuer",
            committed_exposure: "0",
            target_gap: "700",
            continuous_principal: "200",
            principal: "200",
            outcome: "PARTIALLY_ALLOCATED",
            reasons: ["LIQUIDITY_CAPACITY_EXHAUSTED"],
            legs: []
          }
        ]
      }
    }
  };
  window.history.pushState({}, "", `/reports/${report.report_version_id}`);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(report), {
        status: 200,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store" }
      })
    )
  );
  render(<App />);
  const region = await screen.findByRole("region", { name: "Candidate allocation" });
  expect(region).toHaveTextContent("Partially allocated");
  expect(region).toHaveTextContent("Original candidate conclusions retained");
  expect(region).toHaveTextContent(member.calibrated_probability);
  expect(region).toHaveTextContent("LIQUIDITY_CAPACITY_EXHAUSTED");
  expect(region).toHaveTextContent("3890");
  expect(within(region).queryByRole("button")).not.toBeInTheDocument();
});
