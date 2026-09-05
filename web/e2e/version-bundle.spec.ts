import { expect, test } from "@playwright/test";

test("retains the diagnostic version bundle route", async ({ page }) => {
  await page.route("**/api/v1/diagnostics/version", async (route) => {
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        application_version: "0.1.0.dev0",
        source_sha: "a".repeat(40),
        m_agent_version: "0.5.0",
        m_agent_wheel_url:
          "https://github.com/lingdu0007/M-Agent/releases/download/v0.5.0/m_agent-0.5.0-py3-none-any.whl",
        m_agent_wheel_sha256: "8c2592715e840f5d8da4ce239c663864d0c24a16fa05edcefef09071c4fb59a6",
        m_agent_release_commit: "743651e5c74a4865f25a31dab68d188b5b0aed64"
      })
    });
  });

  await page.goto("/");

  await expect(page.getByText("0.1.0.dev0")).toBeVisible();
  await expect(page.getByText("0.5.0", { exact: true })).toBeVisible();
  await expect(page.getByText("a".repeat(40))).toBeVisible();
});
