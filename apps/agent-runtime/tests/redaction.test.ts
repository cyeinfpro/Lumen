import { describe, expect, it, vi } from "vitest";

import { logRuntime, redact } from "../src/redaction.js";

describe("Runtime redaction", () => {
  it("removes credentials, capabilities, prompts, and image data recursively", () => {
    expect(
      redact({
        api_key: "secret-key",
        nested: { capability: "token", prompt: "private prompt" },
        data_base64: "private-image",
        email: "person@example.com",
      }),
    ).toEqual({
      api_key: "[redacted]",
      nested: { capability: "[redacted]", prompt: "[redacted]" },
      data_base64: "[redacted]",
      email: "[email]",
    });
  });

  it("never writes secret fields to structured logs", () => {
    const output = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    logRuntime("warn", "test", { api_key: "secret", run_id: "run-1" });
    expect(output).toHaveBeenCalledOnce();
    expect(output.mock.calls[0]?.[0]).not.toContain("secret");
    expect(output.mock.calls[0]?.[0]).toContain("run-1");
    output.mockRestore();
  });
});
