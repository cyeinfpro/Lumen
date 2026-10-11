import assert from "node:assert/strict";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";

const { ApiError } = await import(new URL("../api/errors.ts", import.meta.url).href);
const {
  canRetainConfirmedIdentity,
  isRetryableIdentityError,
  isUnauthorizedIdentityError,
} = await import(new URL("./identityRecoveryPolicy.ts", import.meta.url).href);

const apiError = (status: number) => new ApiError({ status, code: "test", message: "test" });

test("identity recovery retries only transient API statuses", () => {
  for (const status of [0, 408, 429, 500, 503, 599]) {
    assert.equal(isRetryableIdentityError(apiError(status)), true, String(status));
  }
  for (const status of [200, 400, 401, 403, 404, 409, 422, 499, 600]) {
    assert.equal(isRetryableIdentityError(apiError(status)), false, String(status));
  }
  assert.equal(isUnauthorizedIdentityError(apiError(401)), true);
  assert.equal(isUnauthorizedIdentityError(apiError(403)), false);
});

test("identity recovery rejects aborted requests but retries transport errors", () => {
  const aborted = new Error("cancelled");
  aborted.name = "AbortError";
  assert.equal(isRetryableIdentityError(aborted), false);
  assert.equal(canRetainConfirmedIdentity(aborted, "user-a", "user-a"), false);
  assert.equal(isRetryableIdentityError(new TypeError("network failed")), true);
});

test("identity retention requires the same confirmed user and retryable error", () => {
  const transient = apiError(503);
  assert.equal(canRetainConfirmedIdentity(transient, "user-a", "user-a"), true);
  assert.equal(canRetainConfirmedIdentity(transient, "user-a", "user-b"), false);
  assert.equal(canRetainConfirmedIdentity(transient, null, "user-a"), false);
  assert.equal(canRetainConfirmedIdentity(transient, "user-a", null), false);
  assert.equal(canRetainConfirmedIdentity(apiError(401), "user-a", "user-a"), false);
  assert.equal(canRetainConfirmedIdentity(apiError(403), "user-a", "user-a"), false);
});
