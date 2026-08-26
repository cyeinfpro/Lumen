import type { RuntimeRequest } from "../contracts.js";


const MAX_DISPATCH_PERMIT_RESPONSE_BYTES = 4096;


export class ProviderDispatchPermitError extends Error {
  constructor(readonly code: string) {
    super("Provider dispatch was not authorized");
    this.name = "ProviderDispatchPermitError";
  }
}

function responseCode(value: unknown): string {
  if (value === null || typeof value !== "object") return "agent_provider_dispatch_denied";
  const root = value as { detail?: unknown; error?: unknown };
  const container =
    root.detail !== null && typeof root.detail === "object"
      ? (root.detail as { error?: unknown })
      : root;
  const error = container.error;
  if (error === null || typeof error !== "object") {
    return "agent_provider_dispatch_denied";
  }
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" && code.length <= 64
    ? code
    : "agent_provider_dispatch_denied";
}

async function readBoundedJson(response: Response): Promise<unknown> {
  const declared = response.headers.get("content-length");
  if (declared !== null) {
    const length = Number(declared);
    if (
      !Number.isSafeInteger(length) ||
      length < 0 ||
      length > MAX_DISPATCH_PERMIT_RESPONSE_BYTES
    ) {
      await response.body?.cancel();
      throw new Error("invalid dispatch permit response length");
    }
  }
  if (response.body === null) {
    throw new Error("missing dispatch permit response body");
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_DISPATCH_PERMIT_RESPONSE_BYTES) {
        await reader.cancel();
        throw new Error("oversized dispatch permit response");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = Buffer.concat(chunks.map((chunk) => Buffer.from(chunk)), total);
  const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  return JSON.parse(text) as unknown;
}

export async function authorizeProviderDispatch(
  request: RuntimeRequest,
  ordinal: number,
  signal?: AbortSignal,
): Promise<void> {
  if (
    request.provider_dispatch_url === undefined ||
    request.provider_dispatch_capability === undefined
  ) {
    return;
  }
  if (signal?.aborted) throw new ProviderDispatchPermitError("agent_cancelled");
  const deadline = AbortSignal.timeout(10_000);
  const combinedSignal = signal ? AbortSignal.any([signal, deadline]) : deadline;
  let response: Response;
  try {
    response = await fetch(request.provider_dispatch_url, {
      method: "POST",
      redirect: "error",
      signal: combinedSignal,
      headers: {
        authorization: `Bearer ${request.provider_dispatch_capability}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        dispatch_ordinal: ordinal,
        execution_epoch: request.execution_epoch,
      }),
    });
  } catch {
    throw new ProviderDispatchPermitError(
      signal?.aborted ? "agent_cancelled" : "agent_provider_dispatch_unavailable",
    );
  }
  let payload: unknown;
  try {
    payload = await readBoundedJson(response);
  } catch {
    throw new ProviderDispatchPermitError("agent_provider_dispatch_invalid");
  }
  if (!response.ok) throw new ProviderDispatchPermitError(responseCode(payload));
  if (
    payload === null ||
    typeof payload !== "object" ||
    (payload as { dispatch_ordinal?: unknown }).dispatch_ordinal !== ordinal ||
    typeof (payload as { permit_id?: unknown }).permit_id !== "string"
  ) {
    throw new ProviderDispatchPermitError("agent_provider_dispatch_invalid");
  }
}
