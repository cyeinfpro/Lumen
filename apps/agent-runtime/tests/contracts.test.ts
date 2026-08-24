import { describe, expect, it } from "vitest";

import { parseRuntimeRequest } from "../src/contracts.js";
import { runtimeRequest } from "./fixtures.js";

describe("Runtime contracts", () => {
  it("accepts the strict Worker envelope", () => {
    const parsed = parseRuntimeRequest(runtimeRequest());
    expect(parsed.run_id).toBe("run-1");
    expect(parsed.version).toBe(2);
    expect("limits" in parsed).toBe(false);
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
