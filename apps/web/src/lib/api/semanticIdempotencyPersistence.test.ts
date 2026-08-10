import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";

const {
  sha256Hex,
} = await import("./semanticIdempotencyPersistence.ts");

test("sha256 fallback preserves browser idempotency digests without SubtleCrypto", async () => {
  const values = [
    "",
    "abc",
    "lumen-video-idempotency",
    "Seedance 2.5 多参考素材",
    "x".repeat(1_000),
  ];

  for (const value of values) {
    const expected = createHash("sha256").update(value).digest("hex");
    assert.equal(await sha256Hex(value, null), expected);
  }
});
