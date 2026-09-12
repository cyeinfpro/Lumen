import { describe, expect, it } from "vitest";

import type { RuntimeConfig } from "../src/config.js";
import { parseRuntimeRequest } from "../src/contracts.js";
import { RuntimeReadiness } from "../src/health.js";
import { runtimeRequest, TEST_SECRET } from "./fixtures.js";

function readiness(secret = TEST_SECRET): RuntimeReadiness {
  // The readiness probe reads only this field; no real environment credentials.
  return new RuntimeReadiness({ sharedSecret: secret } as RuntimeConfig);
}

describe("runtime lifecycle and capability boundary audit", () => {
  it("keeps draining terminal even when an older readiness probe finishes later", async () => {
    const probe = readiness();
    const pending = probe.check();
    probe.markDraining();
    expect(await pending).toMatchObject({ ready: false, errorCode: "agent_runtime_draining" });
    expect(await probe.check()).toMatchObject({ ready: false, errorCode: "agent_runtime_draining" });
  });

  it("does not replace the draining reason with a later failed probe", async () => {
    const probe = readiness("short");
    probe.markDraining();
    expect(await probe.check()).toMatchObject({ ready: false, errorCode: "agent_runtime_draining" });
  });

  it("still reports a healthy running process as ready", async () => {
    expect(await readiness().check()).toMatchObject({ ready: true, errorCode: null });
  });

  for (const [url, capability, budget] of [
    [true, false, false], [false, true, false], [true, true, false],
    [false, false, true], [true, false, true], [false, true, true],
  ]) {
    it(`rejects incomplete provider dispatch bindings: ${String(url)}/${String(capability)}/${String(budget)}`, () => {
      const request = {
        ...runtimeRequest(),
        ...(url ? { provider_dispatch_url: "http://api:8000/internal/provider-dispatch" } : {}),
        ...(capability ? { provider_dispatch_capability: "capability-test-token-with-more-than-32-characters" } : {}),
        ...(budget ? { safety_budget: { max_provider_dispatches: 8 } } : {}),
      };
      expect(() => parseRuntimeRequest(request)).toThrow(/dispatch bindings/u);
    });
  }

  for (const slot of ["tool_gateway_url", "tool_capability"] as const) {
    it(`rejects a half-configured inactive image tool: ${slot}`, () => {
      const base = runtimeRequest();
      expect(() => parseRuntimeRequest({
        ...base,
        allowed_tools: [],
        tool_gateway_url: null,
        tool_capability: null,
        [slot]: base[slot],
      })).toThrow(/tool gateway/u);
    });
  }

  it("keeps legacy no-dispatch and fully bound HTTP-plus-port requests valid", () => {
    expect(() => parseRuntimeRequest(runtimeRequest())).not.toThrow();
    expect(() => parseRuntimeRequest({
      ...runtimeRequest(),
      provider_dispatch_url: "http://api:8000/internal/provider-dispatch",
      provider_dispatch_capability: "capability-test-token-with-more-than-32-characters",
      safety_budget: { max_provider_dispatches: 8 },
    })).not.toThrow();
  });
});
