import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { focusManager } from "@tanstack/react-query";
import fixture from "../../tests/fixtures/synthetic/allocation_workspace.json";
import { App } from "./App";
import type { CandidateWorkspace, CandidateWorkspaceCommand } from "./api/client";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  focusManager.setFocused(undefined);
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
  let failRead = false;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      if (request.method === "POST") {
        requests.push(await request.json());
        throw new TypeError("connection interrupted");
      }
      if (failRead)
        return new Response("{}", { status: 503, headers: { "Content-Type": "application/json" } });
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
  failRead = true;
  focusManager.setFocused(false);
  focusManager.setFocused(true);
  expect(await screen.findByText("Candidate results unavailable")).toBeVisible();
  failRead = false;
  focusManager.setFocused(false);
  focusManager.setFocused(true);
  expect(
    await screen.findByRole("button", { name: "Reconcile original submission" })
  ).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile original submission" }));
  await vi.waitFor(() => expect(requests).toHaveLength(2));
  expect(requests[1]).toEqual(requests[0]);
});

it("allows another pending declaration after expiry while new capacity actions remain stopped", async () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2042-05-24T16:01:00Z"));
  const workspace = structuredClone(fixture.workspace) as unknown as CandidateWorkspace;
  const view = workspace.allocations[0];
  const accepted = structuredClone(view.plan_report);
  accepted.event_id = "synthetic-ui-confirmation";
  accepted.result.candidate_allocation = null;
  accepted.result.candidate_confirmation = {
    disposition: "CONFIRMED",
    reasons: [],
    choices: view.allocation.rows.map((row) => ({
      security_id: row.candidate.security_id,
      choice: "ACCEPT"
    })),
    reservations: [],
    released_reservation_ids: [],
    actionable: false
  };
  view.confirmations = [accepted];
  view.new_actions_permitted = false;
  view.stop_reasons = ["ENTRY_WINDOW_CLOSED", "EXECUTION_PENDING_RECONCILIATION"];
  const requests: CandidateWorkspaceCommand[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      if (request.method === "POST") {
        requests.push(await request.json());
        return new Response(JSON.stringify({ report: accepted }), {
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
  const security = await screen.findByRole("combobox", { name: "Declared security" });
  expect(security).toBeEnabled();
  fireEvent.change(security, { target: { value: view.allocation.rows[0].candidate.security_id } });
  fireEvent.change(screen.getByRole("combobox", { name: "Declared status" }), {
    target: { value: "CANCELLED" }
  });
  fireEvent.click(screen.getByRole("button", { name: "Save pending declaration" }));
  await vi.waitFor(() => expect(requests).toHaveLength(1));
  expect(requests[0].operation).toBe("DECLARE");
  expect(requests[0].declaration?.status).toBe("CANCELLED");
  expect(screen.getByRole("button", { name: "Confirm complete batch" })).toBeDisabled();
});
