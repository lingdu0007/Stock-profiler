import "@testing-library/jest-dom/vitest";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { CandidateWorkspace } from "./api/client";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

it("opens the authenticated candidate workspace with explicit empty history", async () => {
  const methods: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: Request) => {
      methods.push(request.method);
      return new Response(
        JSON.stringify({
          observed_at: "2042-07-01T02:00:00Z",
          releases: [],
          details: [],
          current_report_ids: []
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    })
  );
  window.history.pushState({}, "", "/candidates");
  render(<App />);
  expect(await screen.findByRole("heading", { name: "Candidate workspace" })).toBeVisible();
  expect(await screen.findByText("No saved candidate results.")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: /confirm|accept|execute|reserve/i })
  ).not.toBeInTheDocument();
  expect(methods).toEqual(["GET"]);
});

it("opens an original detail without redirecting a superseded report", async () => {
  const fixture = await import("../../tests/fixtures/synthetic/candidate_workspace.json");
  const workspace = structuredClone(fixture.default.workspace) as CandidateWorkspace;
  workspace.releases[0].status = "SUPERSEDED";
  workspace.releases[0].status_reasons = ["EVIDENCE_INVALIDATED"];
  workspace.releases[0].superseded_by_report_id = "synthetic-candidate-replacement";
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(workspace), { headers: { "Content-Type": "application/json" } })
      )
  );
  window.history.pushState({}, "", "/candidates/details/synthetic-candidate-detail");
  render(<App />);
  expect(await screen.findByText(/Historical result/)).toBeVisible();
  expect(await screen.findByText("Invented synthetic research thesis.")).toBeVisible();
  expect(screen.getByRole("link", { name: "Read replacement release" })).toHaveAttribute(
    "href",
    "/candidates/releases/synthetic-candidate-replacement"
  );
  expect(screen.getByRole("link", { name: "Read parent release" })).toHaveAttribute(
    "href",
    "/candidates/releases/synthetic-candidate-report"
  );
  expect(screen.getByText(/2042-07-07, 23:00:00 Asia\/Shanghai/)).toBeVisible();
  expect(
    screen.queryByRole("button", { name: /confirm|accept|execute|reserve/i })
  ).not.toBeInTheDocument();
});

it("returns through the shared sign-in flow for a candidate deep link", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response("{}", { status: 401, headers: { "Content-Type": "application/json" } })
      )
  );
  window.history.pushState({}, "", "/candidates/releases/synthetic-candidate-report");
  render(<App />);
  expect(await screen.findByRole("button", { name: /continue with passkey/i })).toBeVisible();
  expect(window.location.search).toContain(
    "next=%2Fcandidates%2Freleases%2Fsynthetic-candidate-report"
  );
});

it("marks a legacy report deep link with its current withdrawal state", async () => {
  const fixture = await import("../../tests/fixtures/synthetic/candidate_workspace.json");
  const workspace = structuredClone(fixture.default.workspace) as CandidateWorkspace;
  workspace.releases[0].status = "EXPIRED";
  workspace.releases[0].status_reasons = ["CANDIDATE_WINDOW_ENDED"];
  const { syntheticReport } = await import("./test-support/synthetic-report");
  const report = {
    ...syntheticReport,
    report_version_id: "synthetic-candidate-report",
    result: { ...syntheticReport.result, candidate_release: workspace.releases[0].release }
  };
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockImplementation(
        async (request: Request) =>
          new Response(
            JSON.stringify(request.url.endsWith("/api/v1/candidates") ? workspace : report),
            { headers: { "Content-Type": "application/json" } }
          )
      )
  );
  window.history.pushState({}, "", "/reports/synthetic-candidate-report");
  render(<App />);
  expect(await screen.findByText(/Historical result. This release cannot/)).toBeVisible();
  expect(screen.getByRole("link", { name: "Read candidate release status" })).toHaveAttribute(
    "href",
    "/candidates/releases/synthetic-candidate-report"
  );
});

it("rechecks withdrawal when navigating from a release to its security detail", async () => {
  const { fireEvent } = await import("@testing-library/react");
  const fixture = await import("../../tests/fixtures/synthetic/candidate_workspace.json");
  const workspace = structuredClone(fixture.default.workspace) as CandidateWorkspace;
  let reads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async () => {
      reads += 1;
      const result = structuredClone(workspace);
      if (reads > 1) {
        result.releases[0].status = "INVALIDATED";
        result.releases[0].status_reasons = ["AUTHORIZATION_REVOKED"];
      }
      return new Response(JSON.stringify(result), {
        headers: { "Content-Type": "application/json" }
      });
    })
  );
  window.history.pushState({}, "", "/candidates/releases/synthetic-candidate-report");
  render(<App />);
  fireEvent.click(await screen.findByRole("link", { name: "SYNTH-CANDIDATE" }));
  expect(await screen.findByText(/Historical result. This release cannot/)).toBeVisible();
  expect(reads).toBe(2);
});

it("shows frozen population and every result count with reason entrances", async () => {
  const fixture = await import("../../tests/fixtures/synthetic/candidate_workspace.json");
  const workspace = structuredClone(fixture.default.workspace) as CandidateWorkspace;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(
      async () =>
        new Response(JSON.stringify(workspace), {
          headers: { "Content-Type": "application/json" }
        })
    )
  );
  window.history.pushState({}, "", "/candidates/releases/synthetic-candidate-report");
  render(<App />);
  expect(await screen.findByText("Frozen pool count")).toBeVisible();
  for (const label of [
    "Research completed",
    "Candidate count",
    "Rejected count",
    "Abstained count",
    "Failed count"
  ])
    expect(screen.getByText(label)).toBeVisible();
});

it("withdraws current presentation at the saved absolute expiry without navigation", async () => {
  const fixture = await import("../../tests/fixtures/synthetic/candidate_workspace.json");
  const workspace = structuredClone(fixture.default.workspace) as CandidateWorkspace;
  workspace.releases[0].valid_through = new Date(Date.now() + 500).toISOString();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(
      async () =>
        new Response(JSON.stringify(workspace), {
          headers: { "Content-Type": "application/json" }
        })
    )
  );
  window.history.pushState({}, "", "/candidates/releases/synthetic-candidate-report");
  render(<App />);
  expect(await screen.findByText("CURRENT · CANDIDATES")).toBeVisible();
  expect(await screen.findByText("EXPIRED · CANDIDATES")).toBeVisible();
});
