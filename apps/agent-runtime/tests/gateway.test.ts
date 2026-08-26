import { afterEach, describe, expect, it, vi } from "vitest";

import { createImageGateway } from "../src/tools/gateway.js";
import { runtimeRequest, runtimeRequestV3 } from "./fixtures.js";

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

  it("keeps a committed structured 5xx failure known", async () => {
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
      resultUnknown: false,
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
            prompt: "x",
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
      accepted: { prompt: "x", count: 1 },
    });
  });

  it("applies an independent total deadline through the Gateway fetch", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), {
          once: true,
        });
      })),
    );
    await expect(
      createImageGateway(runtimeRequest(), {
        timeoutMs: 5,
        maxResponseBytes: 64 * 1024,
      })("call-timeout", 0, { prompt: "x" }, undefined),
    ).rejects.toMatchObject({
      code: "agent_tool_result_unknown",
      resultUnknown: true,
    });
  });

  it("rejects declared and streamed oversized receipts", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", {
        status: 200,
        headers: { "content-length": "1025" },
      })),
    );
    const gateway = createImageGateway(runtimeRequest(), {
      timeoutMs: 1000,
      maxResponseBytes: 1024,
    });
    await expect(gateway("call-large", 0, { prompt: "x" }, undefined)).rejects
      .toMatchObject({ resultUnknown: true });

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(new Uint8Array(1024));
          controller.enqueue(new Uint8Array(1));
          controller.close();
        },
      }), { status: 200 })),
    );
    await expect(gateway("call-stream-large", 0, { prompt: "x" }, undefined)).rejects
      .toMatchObject({ resultUnknown: true });
  });

  it("requires v2 receipt identity and always rejects redirects", async () => {
    let redirect: RequestRedirect | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        redirect = init?.redirect;
        return new Response(JSON.stringify({
          generation_ids: ["generation-1"],
          mode: "text_to_image",
          replayed: false,
          accepted: {
            prompt: "x",
            reference_labels: [],
            count: 1,
            aspect_ratio: "1:1",
            quality: "2k",
            render_quality: "high",
            background: "auto",
            output_format: "webp",
          },
        }), { status: 200 });
      }),
    );
    await expect(
      createImageGateway(runtimeRequestV3({ tool_receipt_version: 2 }))(
        "call-identity",
        0,
        { prompt: "x" },
        undefined,
      ),
    ).rejects.toMatchObject({ resultUnknown: true });
    expect(redirect).toBe("error");
  });

  it.each([
    ["  x  ", "x", "888bb3ab498d50374defafb4e9952af4b4190f40423516366fe15ac5f72be4ea"],
    ["\u00a0猫\u3000", "猫", "380b0b1668e7696d31ec335269edbb95076bc16638a67b99a5d59ce195c28001"],
  ])("canonicalizes prompt %j before v2 receipt validation", async (prompt, normalized, hash) => {
    let sentArguments: unknown;
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (typeof init?.body !== "string") throw new Error("expected JSON body");
      const sent: unknown = JSON.parse(init.body);
      if (sent === null || typeof sent !== "object") throw new Error("invalid body");
      sentArguments = (sent as { arguments?: unknown }).arguments;
      return new Response(JSON.stringify({
        generation_ids: ["generation-1"],
        mode: "text_to_image",
        replayed: false,
        accepted: {
          prompt: normalized,
          reference_labels: [],
          count: 1,
          aspect_ratio: "1:1",
          quality: "2k",
          render_quality: "high",
          background: "auto",
          output_format: "webp",
        },
        pi_tool_call_id: "call-normalized",
        ordinal: 0,
        request_hash: hash,
      }), { status: 200 });
    }));

    await expect(createImageGateway(runtimeRequestV3({ tool_receipt_version: 2 }))(
      "call-normalized", 0, { prompt }, undefined,
    )).resolves.toMatchObject({ accepted: { prompt: normalized } });
    expect(sentArguments).toMatchObject({ prompt: normalized });
  });

  it("rejects a blank prompt before sending the Gateway request", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    await expect(createImageGateway(runtimeRequest())(
      "call-blank", 0, { prompt: " \u00a0 " }, undefined,
    )).rejects.toMatchObject({
      code: "agent_tool_preflight_failed",
      resultUnknown: false,
    });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("normalizes post-header timeout to result-unknown", async () => {
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
      new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('{"partial":'));
          init?.signal?.addEventListener("abort", () => {
            controller.error(new DOMException("timed out", "TimeoutError"));
          }, { once: true });
        },
      }), { status: 200 }),
    ));
    await expect(createImageGateway(runtimeRequest(), {
      timeoutMs: 5,
      maxResponseBytes: 1024,
    })("call-body-timeout", 0, { prompt: "x" }, undefined)).rejects.toMatchObject({
      code: "agent_tool_result_unknown",
      resultUnknown: true,
    });
  });

  it("treats malformed and oversized deterministic 422 responses as known", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("not-json", { status: 422 })));
    const gateway = createImageGateway(runtimeRequest(), {
      timeoutMs: 1000,
      maxResponseBytes: 8,
    });
    await expect(gateway("call-422", 0, { prompt: "x" }, undefined)).rejects
      .toMatchObject({ code: "agent_tool_failed", resultUnknown: false });

    vi.stubGlobal("fetch", vi.fn(async () => new Response("too-large", {
      status: 422,
      headers: { "content-length": "9" },
    })));
    await expect(gateway("call-422-large", 0, { prompt: "x" }, undefined)).rejects
      .toMatchObject({ code: "agent_tool_failed", resultUnknown: false });
  });
});
