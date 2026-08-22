import { describe, expect, it } from "vitest";

import { parseRuntimeRequest } from "../src/contracts.js";
import { runtimeRequest } from "./fixtures.js";

describe("Runtime contracts", () => {
  it("accepts the strict Worker envelope", () => {
    expect(parseRuntimeRequest(runtimeRequest()).run_id).toBe("run-1");
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
