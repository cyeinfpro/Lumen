import { describe, expect, it } from "vitest";

import {
  AUTH_NONCE_HEADER,
  AUTH_SIGNATURE_HEADER,
  AUTH_TIMESTAMP_HEADER,
  RuntimeAuthenticator,
  canonicalRuntimeRequest,
  signRuntimeRequest,
} from "../src/auth.js";
import { TEST_SECRET } from "./fixtures.js";

describe("Worker request authentication", () => {
  it("uses the canonical cross-language signing string", () => {
    const body = Buffer.from('{"run_id":"run-1"}', "utf8");
    expect(
      canonicalRuntimeRequest("post", "/v1/runs", "1700000000", "nonce-0123456789", body),
    ).toBe(
      "v1\nPOST\n/v1/runs\n1700000000\nnonce-0123456789\n" +
        "923135756928e8f394c6f67aac4b80ee48f168af81e526267db142176f625896",
    );
  });

  it("accepts one valid request and rejects nonce replay", () => {
    const nowMs = 1_700_000_000_000;
    const timestamp = "1700000000";
    const nonce = "nonce-0123456789";
    const body = Buffer.from("{}", "utf8");
    const signature = signRuntimeRequest(
      TEST_SECRET,
      "POST",
      "/v1/runs",
      timestamp,
      nonce,
      body,
    );
    const headers = {
      [AUTH_TIMESTAMP_HEADER]: timestamp,
      [AUTH_NONCE_HEADER]: nonce,
      [AUTH_SIGNATURE_HEADER]: signature,
    };
    const auth = new RuntimeAuthenticator(TEST_SECRET, 120, 100, 30);
    expect(() => auth.verify("POST", "/v1/runs", headers, body, nowMs)).not.toThrow();
    expect(() => auth.verify("POST", "/v1/runs", headers, body, nowMs)).toThrow(
      expect.objectContaining({ code: "agent_runtime_auth_replayed" }),
    );
  });

  it("rejects body tampering and expired timestamps", () => {
    const timestamp = "1700000000";
    const nonce = "nonce-0123456789";
    const signature = signRuntimeRequest(
      TEST_SECRET,
      "POST",
      "/v1/runs",
      timestamp,
      nonce,
      Buffer.from("{}"),
    );
    const headers = {
      [AUTH_TIMESTAMP_HEADER]: timestamp,
      [AUTH_NONCE_HEADER]: nonce,
      [AUTH_SIGNATURE_HEADER]: signature,
    };
    const auth = new RuntimeAuthenticator(TEST_SECRET, 120, 100, 30);
    expect(() =>
      auth.verify("POST", "/v1/runs", headers, Buffer.from('{"x":1}'), 1_700_000_000_000),
    ).toThrow(expect.objectContaining({ code: "agent_runtime_auth_invalid" }));

    const expired = new RuntimeAuthenticator(TEST_SECRET, 120, 100, 30);
    expect(() =>
      expired.verify("POST", "/v1/runs", headers, Buffer.from("{}"), 1_700_000_100_000),
    ).toThrow(expect.objectContaining({ code: "agent_runtime_auth_expired" }));
  });

  it("fails closed instead of evicting an active nonce when capacity is full", () => {
    const nowMs = 1_700_000_000_000;
    const timestamp = "1700000000";
    const body = Buffer.from("{}", "utf8");
    const auth = new RuntimeAuthenticator(TEST_SECRET, 120, 1, 30);
    const headersFor = (nonce: string) => ({
      [AUTH_TIMESTAMP_HEADER]: timestamp,
      [AUTH_NONCE_HEADER]: nonce,
      [AUTH_SIGNATURE_HEADER]: signRuntimeRequest(
        TEST_SECRET,
        "POST",
        "/v1/runs",
        timestamp,
        nonce,
        body,
      ),
    });
    auth.verify(
      "POST",
      "/v1/runs",
      headersFor("nonce-capacity-0001"),
      body,
      nowMs,
    );
    expect(() =>
      auth.verify(
        "POST",
        "/v1/runs",
        headersFor("nonce-capacity-0002"),
        body,
        nowMs,
      ),
    ).toThrow(
      expect.objectContaining({ code: "agent_runtime_auth_capacity_exhausted" }),
    );
  });

  it("retains a nonce for the complete timestamp acceptance window", () => {
    const nowMs = 1_700_000_000_000;
    const timestamp = "1700000000";
    const nonce = "nonce-long-skew-0001";
    const body = Buffer.from("{}", "utf8");
    const headers = {
      [AUTH_TIMESTAMP_HEADER]: timestamp,
      [AUTH_NONCE_HEADER]: nonce,
      [AUTH_SIGNATURE_HEADER]: signRuntimeRequest(
        TEST_SECRET,
        "POST",
        "/v1/runs",
        timestamp,
        nonce,
        body,
      ),
    };
    const auth = new RuntimeAuthenticator(TEST_SECRET, 30, 100, 300);
    auth.verify("POST", "/v1/runs", headers, body, nowMs);
    expect(() =>
      auth.verify("POST", "/v1/runs", headers, body, nowMs + 31_000),
    ).toThrow(expect.objectContaining({ code: "agent_runtime_auth_replayed" }));
  });
});
