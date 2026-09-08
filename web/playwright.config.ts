import { defineConfig } from "@playwright/test";

const webPort = Number(process.env.STOCK_PROFILER_E2E_WEB_PORT ?? "4173");
const webOrigin = `http://127.0.0.1:${webPort}`;

export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: webOrigin,
    headless: true,
    ignoreHTTPSErrors: true
  },
  webServer: {
    command: `corepack pnpm@10.17.1 dev --host 127.0.0.1 --port ${webPort} --strictPort`,
    url: webOrigin,
    reuseExistingServer: false
  }
});
