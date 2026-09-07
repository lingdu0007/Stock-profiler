import { expect, test, type Page } from "@playwright/test";
import { execFile as executeFile, spawn, type ChildProcess } from "node:child_process";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { request as requestHttp } from "node:http";
import { createServer as createHttpsServer, type Server as HttpsServer } from "node:https";
import { createServer as createNetServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import type { FormalReport } from "../src/api/client";

type DecisionCaseExecution = {
  report: FormalReport;
};

const execFile = promisify(executeFile);
const repositoryRoot = fileURLToPath(new URL("../..", import.meta.url));
const vitePort = Number(process.env.STOCK_PROFILER_E2E_WEB_PORT ?? "4173");
const proxyPort = Number(process.env.STOCK_PROFILER_E2E_PROXY_PORT ?? "4174");
const browserOrigin = `https://localhost:${proxyPort}`;
const apiReadyTimeoutMilliseconds = 15_000;
let apiPort = 0;
let temporaryDirectory = "";
let report: FormalReport;
let correctionReport: FormalReport;
let portfolioReport: FormalReport;
let concentrationReport: FormalReport;
let shadowReportId = "";
let sessionToken = "";
let csrfToken = "";
let apiProcess: ChildProcess | undefined;
let httpsProxy: HttpsServer | undefined;

test.beforeAll(async () => {
  test.setTimeout(60_000);
  temporaryDirectory = await mkdtemp(join(tmpdir(), "stock-profiler-playwright-"));
  const applicationDatabase = join(temporaryDirectory, "application.sqlite3");
  const environment = {
    ...process.env,
    STOCK_PROFILER_APP_DATABASE_URL: `sqlite:///${applicationDatabase}`,
    STOCK_PROFILER_AUTH_ORIGIN: browserOrigin,
    STOCK_PROFILER_AUTH_RP_ID: "localhost",
    STOCK_PROFILER_ENVIRONMENT: "test",
    STOCK_PROFILER_REPORT_ACCOUNT_IDS: JSON.stringify([
      "synthetic-account-4017",
      "synthetic-account-8029",
      "synthetic-account-9031",
      "synthetic-account-margin-2001"
    ]),
    STOCK_PROFILER_REPORT_PERMISSIONS: JSON.stringify(["REPORT_READ", "USER_FACT"]),
    STOCK_PROFILER_M_AGENT_RUN_STORE_PATH: join(temporaryDirectory, "m-agent-runs.sqlite3"),
    STOCK_PROFILER_SOURCE_SHA: "a".repeat(40)
  };
  await execFile("uv", ["run", "alembic", "upgrade", "head"], {
    cwd: repositoryRoot,
    env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "migrate" }
  });
  const execution = await execFile("uv", ["run", "stock-profiler", "decision-case-run"], {
    cwd: repositoryRoot,
    env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "cli" }
  });
  report = (JSON.parse(execution.stdout) as DecisionCaseExecution).report;
  const frozenCase = JSON.parse(
    await readFile(
      join(repositoryRoot, "tests/fixtures/synthetic/replayable_frozen_decision_case.json"),
      "utf8"
    )
  ) as { business_identity: string };
  const correction = await execFile(
    "uv",
    [
      "run",
      "stock-profiler",
      "decision-case-correct",
      "--business-identity",
      frozenCase.business_identity
    ],
    { cwd: repositoryRoot, env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "cli" } }
  );
  correctionReport = (JSON.parse(correction.stdout) as DecisionCaseExecution).report;
  const portfolioCasePath = join(temporaryDirectory, "synthetic-portfolio-case.json");
  await execFile(
    "uv",
    [
      "run",
      "python",
      "-c",
      [
        "import json",
        "import sys",
        "from pathlib import Path",
        "from stock_profiler.bootstrap.settings import load_settings",
        "from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case",
        "settings = load_settings()",
        "proposal = json.loads(",
        "    Path('tests/fixtures/synthetic/portfolio_authorization.json').read_text(",
        "        encoding='utf-8'",
        "    )",
        ")['proposal']",
        "cutoff = '2025-05-17T16:00:00Z'",
        "proposal['snapshot']['snapshot_id'] = 'synthetic-portfolio-snapshot-browser-acceptance'",
        "proposal['snapshot']['cutoff_at'] = cutoff",
        "for account in proposal['snapshot']['accounts']:",
        "    account['captured_at'] = cutoff",
        "proposal['risk_budget']['effective_at'] = cutoff",
        "proposal['risk_budget']['expires_at'] = '2025-11-17T16:00:00Z'",
        "proposal['cash_obligations'][0]['latest_usable_at'] = '2025-08-17T16:00:00Z'",
        "case = load_frozen_decision_case(settings).model_dump(mode='json')",
        "case['business_identity'] = 'synthetic:portfolio:browser-acceptance'",
        "case['case_id'] = 'd0-portfolio-browser-acceptance'",
        "case['version_bundle'].update(",
        "    case_contract_version='6.0.0',",
        "    host_contract_version='6.0.0',",
        "    report_projection_contract_version='6.0.0',",
        "    agent_definition_version='2.0.0'",
        ")",
        "case['agent_definition']['version'] = '2.0.0'",
        "case['access_scope'] = {",
        "    'contract_version': '1.0.0',",
        "    'user_id': 'stock-profiler-single-user',",
        "    'account_ids': [account['account_id'] for account in proposal['snapshot']['accounts']],",
        "    'visibility': 'USER'",
        "}",
        "case['knowledge_cutoff'] = cutoff",
        "case['portfolio'] = {",
        "    'operation': 'PORTFOLIO_CONFIRM',",
        "    'proposal': proposal,",
        "    'previous_authorization_id': None,",
        "    'confirmation': {",
        "        'confirmation_id': 'synthetic-risk-confirmation-alpha',",
        "        'user_id': 'stock-profiler-single-user',",
        "        'portfolio_id': proposal['portfolio_id'],",
        "        'snapshot_id': proposal['snapshot']['snapshot_id'],",
        "        'risk_budget_version_id': proposal['risk_budget']['version_id'],",
        "        'confirmed_at': cutoff,",
        "        'confirmed': True",
        "    }",
        "}",
        "Path(sys.argv[1]).write_text(json.dumps(case), encoding='utf-8')"
      ].join("\n"),
      portfolioCasePath
    ],
    { cwd: repositoryRoot, env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "cli" } }
  );
  const portfolioExecution = await execFile(
    "uv",
    ["run", "stock-profiler", "decision-case-run", "--case", portfolioCasePath],
    { cwd: repositoryRoot, env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "cli" } }
  );
  portfolioReport = (JSON.parse(portfolioExecution.stdout) as DecisionCaseExecution).report;
  const concentrationCasePath = join(temporaryDirectory, "synthetic-concentration-case.json");
  await execFile(
    "uv",
    ["run", "python", "tests/integration/concentration_browser_fixture.py", concentrationCasePath],
    { cwd: repositoryRoot, env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "api" } }
  );
  const concentrationExecution = await execFile(
    "uv",
    ["run", "stock-profiler", "decision-case-run", "--case", concentrationCasePath],
    { cwd: repositoryRoot, env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "cli" } }
  );
  concentrationReport = (JSON.parse(concentrationExecution.stdout) as DecisionCaseExecution).report;
  const shadow = await execFile(
    "uv",
    [
      "run",
      "python",
      "-c",
      [
        "from stock_profiler.bootstrap.settings import load_settings",
        "from stock_profiler.bootstrap.decision_cases import run_frozen_decision_case",
        "from stock_profiler.modules.decision_cases.domain import load_frozen_decision_case",
        "settings = load_settings()",
        "case = load_frozen_decision_case(settings).model_dump(mode='json')",
        "case['business_identity'] = 'synthetic:decision:browser-shadow:4519'",
        "case['access_scope'] = {'contract_version': '1.0.0',",
        "    'user_id': 'stock-profiler-single-user',",
        "    'account_ids': ['synthetic-account-4017'], 'visibility': 'SHADOW'}",
        "case['version_bundle'].update(case_contract_version='3.0.0',",
        "    host_contract_version='3.0.0', report_projection_contract_version='3.0.0',",
        "    agent_definition_version='2.0.0')",
        "case['agent_definition']['version'] = '2.0.0'",
        "print(run_frozen_decision_case(settings, case).report_version_id)"
      ].join("\n")
    ],
    { cwd: repositoryRoot, env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "cli" } }
  );
  shadowReportId = shadow.stdout.trim();
  const session = await execFile(
    "uv",
    [
      "run",
      "python",
      "-c",
      [
        "import json",
        "from stock_profiler.adapters.authentication.passkeys import PasskeyAuthenticator",
        "from stock_profiler.adapters.persistence.runtime_ownership import initialize_runtime_storage",
        "from stock_profiler.bootstrap.settings import load_settings",
        "settings = load_settings()",
        "session_token, csrf_token = PasskeyAuthenticator(",
        "    initialize_runtime_storage(settings).engine, settings",
        ")._create_session('playwright-synthetic-passkey')",
        "print(json.dumps({'session_token': session_token, 'csrf_token': csrf_token}))"
      ].join("\n")
    ],
    { cwd: repositoryRoot, env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "api" } }
  );
  ({ session_token: sessionToken, csrf_token: csrfToken } = JSON.parse(session.stdout) as {
    csrf_token: string;
    session_token: string;
  });
  apiPort = await availableLoopbackPort();
  apiProcess = spawn(
    "uv",
    [
      "run",
      "uvicorn",
      "stock_profiler.entrypoints.http.app:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(apiPort),
      "--no-access-log"
    ],
    { cwd: repositoryRoot, env: { ...environment, STOCK_PROFILER_PROCESS_ROLE: "api" } }
  );
  await waitForApi();
  await startHttpsProxy();
});

test.afterAll(async () => {
  await stopHttpsProxy();
  await stopApi();
  if (temporaryDirectory) {
    await rm(temporaryDirectory, { force: true, recursive: true });
  }
});

test("shows the committed CLI report from the authenticated API on every page load", async ({
  page
}) => {
  await installAuthenticatedSession(page);
  const refreshResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/v1/auth/session/refresh"
  );
  const firstReportResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === `/api/v1/reports/${report.report_version_id}`
  );

  await page.goto(reportUrl());
  expect((await refreshResponse).status()).toBe(200);
  const receivedReport = await firstReportResponse;
  expect(receivedReport.status()).toBe(200);
  expect(receivedReport.headers()["cache-control"]).toBe("no-store");
  expect(await receivedReport.json()).toEqual(report);
  await expect(page.getByText("SYNTHETIC_REVIEW_COMPLETE")).toBeVisible();
  await expect(page.getByText(report.report_version_id)).toBeVisible();
  await expect(page.getByText(report.event_id)).toBeVisible();
  await expect(page.getByText(report.framework_run_id)).toBeVisible();
  await expect(page.getByText("D0 synthetic", { exact: true })).toBeVisible();

  const reloadedReportResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === `/api/v1/reports/${report.report_version_id}`
  );
  await page.reload();
  expect((await reloadedReportResponse).status()).toBe(200);
  await expect(page.getByText("SYNTHETIC_REVIEW_COMPLETE")).toBeVisible();
});

test("projects the versioned portfolio CLI case through the authenticated PWA", async ({
  page
}) => {
  await installAuthenticatedSession(page);
  const receivedReport = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api/v1/reports/${portfolioReport.report_version_id}`
  );

  await page.goto(`${browserOrigin}/reports/${portfolioReport.report_version_id}`);

  const response = await receivedReport;
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toBe("no-store");
  expect(await response.json()).toEqual(portfolioReport);
  const authorization = page.getByRole("region", { name: "Portfolio authorization" });
  await expect(authorization).toBeVisible();
  await expect(authorization.getByText("APPROVED", { exact: true })).toBeVisible();
  const confirmation = page.getByRole("region", { name: "Portfolio confirmation" });
  await expect(confirmation).toBeVisible();
  await expect(confirmation.getByText("synthetic-risk-budget-alpha")).toBeVisible();
  await expect(confirmation.getByText("synthetic-cash-obligation-alpha")).toBeVisible();
  await expect(confirmation.getByText("synthetic-action-policy-alpha")).toBeVisible();
  await expect(
    page.getByRole("button", { name: /generate|prefill|submit|modify|cancel order/i })
  ).toHaveCount(0);
});

test("does not reveal a report when the API returns an unauthenticated response", async ({
  page
}) => {
  const deniedResponse = page.waitForResponse(
    (response) => new URL(response.url()).pathname === `/api/v1/reports/${report.report_version_id}`
  );

  await page.goto(reportUrl());

  expect((await deniedResponse).status()).toBe(401);
  await expect(page.getByRole("button", { name: "Continue with Passkey" })).toBeVisible();
  await expect(page.getByText("SYNTHETIC_REVIEW_COMPLETE")).not.toBeVisible();
});

test("retains the CLI concentration obligation in authenticated desktop and mobile reports", async ({
  page
}, testInfo) => {
  await installAuthenticatedSession(page);
  expect(concentrationReport.result.concentration?.disposition).toBe("ASSESSED");
  const issuer = concentrationReport.result.concentration!.issuers[0];
  const target = issuer.targets[0];
  for (const width of [1280, 320]) {
    await page.setViewportSize({ width, height: 900 });
    const received = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname ===
        `/api/v1/reports/${concentrationReport.report_version_id}`
    );
    await page.goto(`${browserOrigin}/reports/${concentrationReport.report_version_id}`);
    expect(await (await received).json()).toEqual(concentrationReport);
    const concentration = page.getByRole("region", { name: "Issuer concentration" });
    await expect(concentration).toBeVisible();
    await expect(concentration.getByText("REDUCE", { exact: true })).toBeVisible();
    await expect(
      concentration.getByText(
        `Target quantity ${target.target_quantity}; required reduction ${target.required_reduction_quantity}`,
        { exact: true }
      )
    ).toBeVisible();
    await expect(
      concentration.getByText(String(issuer.exposure_gap), { exact: true })
    ).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
    ).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath(`concentration-${width}.png`),
      fullPage: true
    });
  }
});

test("retains original and corrected evidence across desktop and mobile views", async ({
  page
}, testInfo) => {
  await installAuthenticatedSession(page);
  for (const width of [1280, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${browserOrigin}/reports/${correctionReport.report_version_id}`);
    const correction = page.getByRole("region", { name: "Correction evidence" });
    await expect(correction).toBeVisible();
    await expect(
      correction.getByText(
        "The fictional issuer has not completed the imaginary orbital-mosaic checklist.",
        { exact: true }
      )
    ).toBeVisible();
    await expect(correction.getByText("2042-05-17T16:01:50Z")).toBeVisible();
    await expect(page.getByText(report.framework_run_id, { exact: true })).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)
    ).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`correction-${width}.png`), fullPage: true });
    await page.goto(reportUrl());
    await expect(page.getByText("SYNTHETIC_REVIEW_COMPLETE")).toBeVisible();
    await expect(page.getByRole("region", { name: "Correction evidence" })).toHaveCount(0);
    await expect(page.getByText(report.knowledge_cutoff, { exact: true })).toBeVisible();
  }
});

test("keeps shadow content and order actions absent on desktop and mobile", async ({
  page
}, testInfo) => {
  await installAuthenticatedSession(page);
  for (const width of [1280, 320]) {
    await page.setViewportSize({ width, height: 900 });
    const denied = page.waitForResponse(
      (response) => new URL(response.url()).pathname === `/api/v1/reports/${shadowReportId}`
    );
    await page.goto(`${browserOrigin}/reports/${shadowReportId}`);
    const response = await denied;
    expect(response.status()).toBe(404);
    expect(response.headers()["cache-control"]).toBe("no-store");
    await expect(page.getByText("Report unavailable", { exact: true })).toBeVisible();
    await expect(page.getByRole("region", { name: "Formal report", exact: true })).toHaveCount(0);
    await expect(page.getByText("SYNTHETIC_REVIEW_COMPLETE")).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: /generate|prefill|submit|modify|cancel order/i })
    ).toHaveCount(0);
    const cacheNames = await page.evaluate(() => caches.keys());
    for (const name of cacheNames) {
      const paths = await page.evaluate(async (cacheName) => {
        const cache = await caches.open(cacheName);
        return (await cache.keys()).map((request) => new URL(request.url).pathname);
      }, name);
      expect(paths.filter((path) => path.startsWith("/api/"))).toEqual([]);
    }
    await page.screenshot({ path: testInfo.outputPath(`shadow-${width}.png`), fullPage: true });
  }
});

async function installAuthenticatedSession(page: Page) {
  await page.context().addCookies([
    {
      name: "__Host-stock_profiler_session",
      value: sessionToken,
      url: browserOrigin,
      httpOnly: true,
      sameSite: "Lax",
      secure: true
    },
    {
      name: "__Host-stock_profiler_csrf",
      value: csrfToken,
      url: browserOrigin,
      httpOnly: false,
      sameSite: "Lax",
      secure: true
    }
  ]);
}

function reportUrl() {
  return `${browserOrigin}/reports/${report.report_version_id}`;
}

async function waitForApi() {
  const retryDelayMilliseconds = 100;
  const maximumAttempts = apiReadyTimeoutMilliseconds / retryDelayMilliseconds;
  for (let attempt = 0; attempt < maximumAttempts; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${apiPort}/readyz`);
      if (response.ok && (await response.json()).status === "ready") {
        return;
      }
    } catch {
      if (apiProcess?.exitCode !== null) {
        throw new Error(`FastAPI process exited with code ${apiProcess.exitCode}.`);
      }
    }
    await new Promise((resolve) => setTimeout(resolve, retryDelayMilliseconds));
  }
  throw new Error("FastAPI test server did not become ready.");
}

async function startHttpsProxy() {
  const certificatePath = join(temporaryDirectory, "playwright-proxy-certificate.pem");
  const privateKeyPath = join(temporaryDirectory, "playwright-proxy-private-key.pem");
  await execFile("openssl", [
    "req",
    "-x509",
    "-newkey",
    "rsa:2048",
    "-nodes",
    "-days",
    "1",
    "-keyout",
    privateKeyPath,
    "-out",
    certificatePath,
    "-subj",
    "/CN=localhost",
    "-addext",
    "subjectAltName=DNS:localhost"
  ]);
  httpsProxy = createHttpsServer(
    {
      cert: await readFile(certificatePath),
      key: await readFile(privateKeyPath)
    },
    (request, response) => {
      const upstreamPort = request.url?.startsWith("/api/") ? apiPort : vitePort;
      const upstream = requestHttp(
        {
          headers: { ...request.headers, host: `127.0.0.1:${upstreamPort}` },
          host: "127.0.0.1",
          method: request.method,
          path: request.url,
          port: upstreamPort
        },
        (upstreamResponse) => {
          response.writeHead(upstreamResponse.statusCode ?? 502, upstreamResponse.headers);
          upstreamResponse.pipe(response);
        }
      );
      upstream.on("error", () => {
        response.writeHead(502);
        response.end();
      });
      request.pipe(upstream);
    }
  );
  await new Promise<void>((resolve) => {
    httpsProxy?.listen(proxyPort, "127.0.0.1", resolve);
  });
}

async function availableLoopbackPort() {
  return new Promise<number>((resolve, reject) => {
    const server = createNetServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (address === null || typeof address === "string") {
        reject(new Error("could not allocate a loopback port for the FastAPI test server"));
        return;
      }
      server.close((error) => (error ? reject(error) : resolve(address.port)));
    });
  });
}

async function stopHttpsProxy() {
  if (!httpsProxy) {
    return;
  }
  await new Promise<void>((resolve, reject) => {
    httpsProxy?.close((error) => (error ? reject(error) : resolve()));
  });
}

async function stopApi() {
  if (!apiProcess || apiProcess.exitCode !== null) {
    return;
  }
  const exited = new Promise<void>((resolve) => {
    apiProcess?.once("exit", () => resolve());
  });
  apiProcess.kill("SIGTERM");
  await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 5_000))]);
}
