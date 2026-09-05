import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: "http://127.0.0.1:4173",
    headless: true,
    ignoreHTTPSErrors: true
  },
  webServer: {
    command: "corepack pnpm@10.17.1 dev --host 127.0.0.1 --port 4173 --strictPort",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false
  }
});
