import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/synthetic/allocation_workspace.json";
import { App } from "./App";
import type { CandidateWorkspace, CandidateWorkspaceCommand } from "./api/client";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.pushState({}, "", "/");
});

it("withdraws new actions at the original deadline while retaining saved evidence", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2042-05-24T16:01:00Z"));
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(
      async () =>
        new Response(JSON.stringify(fixture.workspace), {
          headers: { "Content-Type": "application/json" }
        })
    )
  );
  window.history.pushState({}, "", "/candidates");
  render(<App />);
  expect(
    await screen.findByRole("link", { name: "Read original allocation report" })
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Replay current policy" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Confirm complete batch" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Replan within original window" })).toBeDisabled();
  expect(screen.getByText(/New actions stopped.*ENTRY_WINDOW_CLOSED/)).toBeVisible();
});

it("keeps a complete batch draft local and submits saved identities after policy review", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2042-05-19T16:01:00Z"));
  const workspace = structuredClone(fixture.workspace) as unknown as CandidateWorkspace;
  const view = workspace.allocations![0];
  const requests: CandidateWorkspaceCommand[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      if (request.method === "POST") {
        const body = (await request.json()) as CandidateWorkspaceCommand;
        requests.push(body);
        const report = structuredClone(view.plan_report);
        report.result.candidate_allocation = null;
        report.result.candidate_confirmation = {
          disposition: "REVALIDATED",
          reasons: [],
          choices: [],
          reservations: [],
          released_reservation_ids: [],
          actionable: false
        };
        return new Response(JSON.stringify({ report }), {
          headers: { "Content-Type": "application/json" }
        });
      }
      return new Response(JSON.stringify(workspace), {
        headers: { "Content-Type": "application/json" }
      });
    })
  );
  window.history.pushState({}, "", "/candidates");
  render(<App />);
  expect(
    await screen.findByRole("heading", { name: "Personal allocation and execution" })
  ).toBeVisible();
  const selects = screen.getAllByRole("combobox", { name: /Choice for/ });
  fireEvent.change(selects[0], { target: { value: "ACCEPT" } });
  expect(requests).toEqual([]);
  expect(screen.getByRole("button", { name: "Confirm complete batch" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Replay current policy" }));
  expect(await screen.findByText(/Policy replay: REVALIDATED/)).toBeVisible();
  await vi.waitFor(() =>
    expect(screen.getByRole("button", { name: "Confirm complete batch" })).toBeEnabled()
  );
  fireEvent.click(screen.getByRole("button", { name: "Confirm complete batch" }));
  await vi.waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1].plan_report_version_id).toBe(view.report_version_id);
  expect(requests[1].input_report_version_id).toBe(view.latest_input_report_version_id);
  expect(requests[1].choices).toHaveLength(
    view.allocation.rows.filter((row) => Number(row.principal) > 0).length
  );
  expect(requests[1].seen_confirmation_id).toBeNull();
});

it("retains the original submission for reconciliation after an uncertain response", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2042-05-19T16:01:00Z"));
  const workspace = structuredClone(fixture.workspace) as unknown as CandidateWorkspace;
  const requests: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      if (request.method === "POST") {
        requests.push(await request.json());
        throw new TypeError("connection interrupted");
      }
      return new Response(JSON.stringify(workspace), {
        headers: { "Content-Type": "application/json" }
      });
    })
  );
  window.history.pushState({}, "", "/candidates");
  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "Replay current policy" }));
  expect(await screen.findByText(/Submission outcome unknown/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Replan within original window" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile original submission" }));
  await vi.waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1]).toEqual(requests[0]);
});
