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
const browserOrigin = "https://localhost:4174";
const vitePort = 4173;
const proxyPort = 4174;
let apiPort = 0;
let temporaryDirectory = "";
let report: FormalReport;
let sessionToken = "";
let csrfToken = "";
let apiProcess: ChildProcess | undefined;
let httpsProxy: HttpsServer | undefined;

test.beforeAll(async () => {
  temporaryDirectory = await mkdtemp(join(tmpdir(), "stock-profiler-playwright-"));
  const applicationDatabase = join(temporaryDirectory, "application.sqlite3");
  const environment = {
    ...process.env,
    STOCK_PROFILER_APP_DATABASE_URL: `sqlite:///${applicationDatabase}`,
    STOCK_PROFILER_AUTH_ORIGIN: browserOrigin,
    STOCK_PROFILER_AUTH_RP_ID: "localhost",
    STOCK_PROFILER_ENVIRONMENT: "test",
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
  for (let attempt = 0; attempt < 50; attempt += 1) {
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
    await new Promise((resolve) => setTimeout(resolve, 100));
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
