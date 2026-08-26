import { afterEach, describe, expect, it, vi } from "vitest";

import type { RuntimeRequest } from "../src/contracts.js";
import {
  authorizeProviderDispatch,
  ProviderDispatchPermitError,
} from "../src/providers/dispatch-permit.js";

function request(): RuntimeRequest {
  return {
    provider_dispatch_url: "http://api:8000/internal/agent/runs/run-1/provider-dispatch",
    provider_dispatch_capability: "dispatch-capability",
    execution_epoch: 3,
  } as RuntimeRequest;
}

function errorCode(error: unknown): string | undefined {
  return error instanceof ProviderDispatchPermitError ? error.code : undefined;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Provider dispatch permit response boundary", () => {
  it("accepts a matching bounded permit response", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({ permit_id: "permit-1", dispatch_ordinal: 1 }),
    );

    await authorizeProviderDispatch(request(), 1);

    expect(fetch).toHaveBeenCalledOnce();
  });

  it("rejects declared overflow before consuming the body", async () => {
    let pulls = 0;
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        controller.enqueue(new Uint8Array(1024));
      },
      cancel() {
        cancelled = true;
      },
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(body, { headers: { "content-length": "4097" } }),
    );

    await expect(authorizeProviderDispatch(request(), 1)).rejects.toSatisfy(
      (error: unknown) => errorCode(error) === "agent_provider_dispatch_invalid",
    );
    expect(pulls).toBeLessThanOrEqual(1);
    expect(cancelled).toBe(true);
  });

  it("cancels a streamed response immediately after the byte ceiling", async () => {
    let pulls = 0;
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        controller.enqueue(new Uint8Array(1024));
      },
      cancel() {
        cancelled = true;
      },
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body));

    await expect(authorizeProviderDispatch(request(), 1)).rejects.toSatisfy(
      (error: unknown) => errorCode(error) === "agent_provider_dispatch_invalid",
    );
    expect(pulls).toBeLessThanOrEqual(6);
    expect(cancelled).toBe(true);
  });
});
