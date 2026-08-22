import { afterEach, describe, expect, it, vi } from "vitest";

import { createImageGateway } from "../src/tools/gateway.js";
import { runtimeRequest } from "./fixtures.js";

describe("Tool Gateway acknowledgement semantics", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads the API top-level error contract and treats 4xx as known", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(
        JSON.stringify({ error: { code: "INSUFFICIENT_BALANCE" } }),
        { status: 402, headers: { "content-type": "application/json" } },
      )),
    );
    await expect(
      createImageGateway(runtimeRequest())("call-1", 0, { prompt: "x" }, undefined),
    ).rejects.toMatchObject({
      code: "INSUFFICIENT_BALANCE",
      resultUnknown: false,
    });
  });

  it("treats every parsed 5xx acknowledgement as result-unknown", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(
        JSON.stringify({ error: { code: "agent_image_provider_unavailable" } }),
        { status: 500, headers: { "content-type": "application/json" } },
      )),
    );
    await expect(
      createImageGateway(runtimeRequest())("call-1", 0, { prompt: "x" }, undefined),
    ).rejects.toMatchObject({
      code: "agent_image_provider_unavailable",
      resultUnknown: true,
    });
  });

  it("requires normalized accepted parameters in successful receipts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(
        JSON.stringify({
          generation_ids: ["generation-1"],
          mode: "text_to_image",
          replayed: false,
          accepted: {
            prompt: "normalized",
            reference_labels: [],
            count: 1,
            aspect_ratio: "1:1",
            quality: "2k",
            render_quality: "high",
            background: "auto",
            output_format: "webp",
          },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      )),
    );
    await expect(
      createImageGateway(runtimeRequest())("call-1", 0, { prompt: "x" }, undefined),
    ).resolves.toMatchObject({
      accepted: { prompt: "normalized", count: 1 },
    });
  });
});
