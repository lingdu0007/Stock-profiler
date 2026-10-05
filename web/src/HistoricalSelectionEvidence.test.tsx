import "@testing-library/jest-dom/vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import fixture from "../../tests/fixtures/synthetic/historical_selection.json";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

it("reads stored historical gates, calendar gaps and paired baselines without authorizing actions", async () => {
  const report = fixture.report;
  window.history.pushState({}, "", `/reports/${report.report_version_id}`);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(report), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    )
  );
  render(<App />);
  const section = await screen.findByRole("region", { name: "Historical selection evidence" });
  expect(section).toHaveTextContent("INSUFFICIENT");
  expect(section).toHaveTextContent("overall_pass");
  expect(section).toHaveTextContent("UNIVERSE");
  expect(section).toHaveTextContent("RANDOM");
  expect(section).toHaveTextContent("FOUR_FACTOR");
  expect(section).toHaveTextContent("BULL");
  expect(section).toHaveTextContent("different_securities");
  expect(section).toHaveTextContent("No recommendation or execution authorization");
  expect(section.querySelector("button")).toBeNull();
});
