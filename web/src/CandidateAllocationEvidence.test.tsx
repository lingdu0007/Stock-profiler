import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import fixture from "../../tests/fixtures/synthetic/allocation_workspace.json";
import { CandidateAllocationEvidence } from "./CandidateAllocationEvidence";
import type { FormalReport } from "./api/client";

afterEach(cleanup);

it("displays the saved synthetic envelope separately from normal capacity and commitments", () => {
  const plan = structuredClone(fixture.workspace.allocations[0].allocation);
  Object.assign(plan, {
    beta: {
      activation_id: "synthetic-envelope-ui",
      permission_envelope: "INITIAL",
      ratio: "0.03",
      net_liquidation_equity: "10000",
      normal_feasible_capacity: "700",
      committed_exposure: "100",
      capacity: "200",
      qualification_decision_ids: ["synthetic-qualification-ui"],
      operations: {
        plan_months: ["2042-04"],
        missing_months: [],
        window_complete: true,
        metrics: {
          confirmation: { required: 0, completed: 0, rate: null, status: "NOT_APPLICABLE" }
        },
        consecutive_core_failure: false,
        safety_failures: [],
        passed: true
      },
      reasons: ["BETA_EXPANSION_OBSERVATION_REQUIRED"],
      evidence_scope: "D0_SYNTHETIC_CONTRACT_ONLY",
      actionable: false
    }
  });
  render(
    <CandidateAllocationEvidence
      plan={plan as unknown as NonNullable<FormalReport["result"]["candidate_allocation"]>}
    />
  );
  expect(screen.getByRole("heading", { name: "Saved coverage envelope" })).toBeVisible();
  expect(screen.getByText("Normal feasible capacity")).toBeVisible();
  expect(screen.getByText("Coverage already committed")).toBeVisible();
  expect(screen.getByText("200")).toBeVisible();
  expect(
    screen.getByText(/Synthetic contract evidence; real activation is unavailable/)
  ).toBeVisible();
  expect(screen.getByText(/confirmation: NOT_APPLICABLE/)).toBeVisible();
  expect(screen.getByText("BETA_EXPANSION_OBSERVATION_REQUIRED")).toBeVisible();
});
