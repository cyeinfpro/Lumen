import { describe, expect, it } from "vitest";

import { parseRuntimeRequest } from "../src/contracts.js";
import { runtimeRequest, runtimeRequestV5 } from "./fixtures.js";

describe("Runtime contracts", () => {
  it("accepts the strict Worker envelope", () => {
    const parsed = parseRuntimeRequest(runtimeRequest());
    expect(parsed.run_id).toBe("run-1");
    expect(parsed.version).toBe(2);
    expect("limits" in parsed).toBe(false);
  });

  it("requires the nullable provider proxy slot on the wire", () => {
    const { proxy_url: _proxyUrl, ...provider } = runtimeRequest().provider;
    void _proxyUrl;
    expect(() =>
      parseRuntimeRequest({
        ...runtimeRequest(),
        provider,
      }),
    ).toThrow(/invalid request/u);
  });

  it("accepts receiver-first v3 typed history and provider metadata", () => {
    const parsed = parseRuntimeRequest({
      ...runtimeRequest(),
      version: 3,
      operation: "prompt",
      tool_receipt_version: 2,
      provider: {
        ...runtimeRequest().provider,
        thinking_level_map: { max: "max" },
      },
      history: [
        {
          message_id: "assistant-history",
          role: "assistant",
          text: "tool turn",
          api: "openai-responses",
          provider_id: "provider-history",
          model: "model-history",
          stop_reason: "toolUse",
          tool_calls: [
            {
              id: "tool-1",
              name: "lumen_create_image",
              arguments: { prompt: "x" },
            },
          ],
          tool_results: [
            {
              tool_call_id: "tool-1",
              name: "lumen_create_image",
              text: '{"status":"succeeded"}',
              is_error: false,
            },
          ],
        },
      ],
    });

    expect(parsed.version).toBe(3);
    expect(parsed.history[0]).toMatchObject({
      provider_id: "provider-history",
      tool_calls: [{ id: "tool-1" }],
    });
  });

  it("accepts v5 first-party search and virtual file tools without a host filesystem", () => {
    const parsed = parseRuntimeRequest(runtimeRequestV5({
      allowed_tools: ["lumen_web_search", "lumen_list_files", "lumen_read_file"],
      workspace_files: [{
        name: "brief.md",
        mime_type: "text/markdown",
        size: 7,
        content: "# Brief",
      }],
      tool_policy: {
        max_image_tool_calls: 0,
        max_images_per_run: 4,
        max_web_search_calls: 3,
        max_file_tool_calls: 8,
        max_tool_calls: 11,
      },
    }));

    expect(parsed.version).toBe(5);
    if (parsed.version !== 5) throw new Error("expected v5 request");
    expect(parsed.allowed_tools).toEqual([
      "lumen_web_search",
      "lumen_list_files",
      "lumen_read_file",
    ]);
    expect(parsed.tool_gateway_url).toBeNull();
    expect(parsed.workspace_files[0]?.content).toBe("# Brief");
  });

  it("accepts legacy v1 envelopes without Pi checkpoint fields", () => {
    const {
      tool_policy: _toolPolicy,
      compaction: _compaction,
      event_features: _eventFeatures,
      ...current
    } = runtimeRequest();
    void _toolPolicy;
    void _compaction;
    void _eventFeatures;
    const legacyEnvelope = {
      ...current,
      version: 1 as const,
      history: [{ role: "user" as const, text: "legacy" }],
      limits: {
        max_turns: 6,
        max_tool_calls: 3,
        max_image_tool_calls: 2,
        max_images_per_run: 4,
        max_output_tokens: 4096,
        run_timeout_seconds: 600,
        tool_timeout_seconds: 30,
        max_output_chars: 262_144,
      },
    };
    const parsed = parseRuntimeRequest(legacyEnvelope);
    expect(parsed.compaction).toBeUndefined();
    expect(parsed.event_features).toBeUndefined();
  });

  it("rejects extra fields and credential-bearing URLs", () => {
    expect(() =>
      parseRuntimeRequest({ ...runtimeRequest(), injected: true }),
    ).toThrow(/invalid request/u);
    expect(() =>
      parseRuntimeRequest(
        runtimeRequest({
          provider: {
            ...runtimeRequest().provider,
            base_url: "https://user:password@provider.example/v1",
          },
        }),
      ),
    ).toThrow(/base URL/u);
  });

  it("requires vision and exact tool gateway bindings", () => {
    const reference = {
      reference_label: "ref_1" as const,
      role: "product",
      display_label: "Product",
      mime_type: "image/png" as const,
      data_base64: Buffer.from("preview").toString("base64"),
    };
    expect(() =>
      parseRuntimeRequest(
        runtimeRequest({
          references: [reference],
          provider: { ...runtimeRequest().provider, vision_supported: false },
        }),
      ),
    ).toThrow(/reference images/u);
    expect(() =>
      parseRuntimeRequest(
        runtimeRequest({
          allowed_tools: [],
          tool_gateway_url: runtimeRequest().tool_gateway_url,
          tool_capability: runtimeRequest().tool_capability,
        }),
      ),
    ).toThrow(/tool gateway/u);
  });

  it("accepts session labels through ref_64 and rejects larger catalogs", () => {
    const reference = {
      reference_label: "ref_64",
      role: "reference",
      display_label: null,
      mime_type: "image/webp",
      data_base64: Buffer.from("preview").toString("base64"),
    } as const;
    expect(
      parseRuntimeRequest(runtimeRequest({ references: [reference] })).references[0]
        ?.reference_label,
    ).toBe("ref_64");
    expect(() =>
      parseRuntimeRequest(
        runtimeRequest({
          references: [
            { ...reference, reference_label: "ref_65" },
          ],
        }),
      ),
    ).toThrow(/invalid request/u);
  });

  it("preserves the legacy v2 reference envelope while v3 enforces turn scope", () => {
    const references = Array.from({ length: 17 }, (_, index) => ({
      reference_label: `ref_${String(index + 1)}`,
      role: "reference",
      display_label: null,
      mime_type: "image/webp",
      data_base64: Buffer.from("preview").toString("base64"),
    }));
    expect(parseRuntimeRequest({ ...runtimeRequest(), references }).version).toBe(2);
    expect(() => parseRuntimeRequest({
      ...runtimeRequest(),
      version: 3,
      operation: "prompt",
      references,
    })).toThrow(/current turn reference limit/u);
  });

  it("requires Pi compaction boundaries to reference retained history", () => {
    const history = [
      { message_id: "message-1", role: "user" as const, text: "old context" },
      { message_id: "message-2", role: "assistant" as const, text: "reply" },
    ];
    expect(
      parseRuntimeRequest(
        runtimeRequest({
          history,
          compaction: {
            summary: "## Goal\nKeep working",
            first_kept_message_id: "message-1",
            next_message_id: "user-message-1",
            tokens_before: 260_000,
          },
        }),
      ).compaction?.first_kept_message_id,
    ).toBe("message-1");
    expect(() =>
      parseRuntimeRequest(
        runtimeRequest({
          history,
          compaction: {
            summary: "missing boundary",
            first_kept_message_id: "message-missing",
            next_message_id: "user-message-1",
            tokens_before: 260_000,
          },
        }),
      ),
    ).toThrow(/compaction boundary/u);
  });

  it("does not combine a proxy path with direct DNS pins", () => {
    expect(() =>
      parseRuntimeRequest(
        runtimeRequest({
          provider: {
            ...runtimeRequest().provider,
            proxy_url: "socks5h://proxy.internal:1080",
            resolved_ips: ["203.0.113.10"],
          },
        }),
      ),
    ).toThrow(/mutually exclusive/u);
  });
});
