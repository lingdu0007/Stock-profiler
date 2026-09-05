import {
  startAuthentication,
  type PublicKeyCredentialRequestOptionsJSON
} from "@simplewebauthn/browser";
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

const client = createClient<paths>({
  baseUrl: "",
  fetch: (request) =>
    globalThis.fetch(request, {
      cache: "no-store",
      credentials: "same-origin"
    })
});

export type FormalReport = components["schemas"]["FormalReport"];

export class ApiResponseError extends Error {
  constructor(readonly status: number) {
    super(`API request failed with status ${status}.`);
  }
}

export async function fetchFormalReport(reportVersionId: string): Promise<FormalReport> {
  const { data, response } = await client.GET("/api/v1/reports/{report_version_id}", {
    params: { path: { report_version_id: reportVersionId } }
  });
  if (!data) {
    throw new ApiResponseError(response.status);
  }
  return data;
}

export async function completePasskeyAuthentication(): Promise<void> {
  const optionsResponse = await client.POST("/api/v1/auth/passkeys/authentication/options");
  if (!optionsResponse.data) {
    throw new ApiResponseError(optionsResponse.response.status);
  }
  const credential = await startAuthentication({
    optionsJSON: optionsResponse.data.options as unknown as PublicKeyCredentialRequestOptionsJSON
  });
  const verifyResponse = await client.POST("/api/v1/auth/passkeys/authentication/verify", {
    body: {
      challenge_id: optionsResponse.data.challenge_id,
      credential: credential as unknown as Record<string, unknown>
    }
  });
  if (!verifyResponse.data) {
    throw new ApiResponseError(verifyResponse.response.status);
  }
}
