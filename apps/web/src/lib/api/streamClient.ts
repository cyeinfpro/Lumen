import { apiUrl } from "./baseUrl";
import { deadline } from "./requestBudget";
import { apiTransport } from "./transport";

const STREAM_RESPONSE_HEADER_TIMEOUT_MS = 30_000;

export const streamClient = {
  url(path: string): string {
    return apiUrl(path);
  },
  postJson(
    path: string,
    body: unknown,
    signal?: AbortSignal,
    idempotencyKey?: string,
  ): Promise<Response> {
    return apiTransport.requestRaw<Response>(
      path,
      {
        method: "POST",
        body: JSON.stringify(body),
        headers: idempotencyKey
          ? { "Idempotency-Key": idempotencyKey }
          : undefined,
        signal,
        requestClass: "command",
        budget: deadline(STREAM_RESPONSE_HEADER_TIMEOUT_MS),
        applyCsrf: true,
      },
      async (response) => response,
    );
  },
};
