import assert from "node:assert/strict";
import test from "node:test";
import { monitoringRoute } from "./monitoringRoute.ts";

test("monitoring routes remove query and fragment credentials", () => {
  assert.equal(monitoringRoute("/agent?token=secret#password", "https://local.test"), "/agent");
});
test("monitoring routes redact token-bearing route segments", () => {
  for (const route of ["invite", "share", "reset-password"]) {
    assert.equal(monitoringRoute(`/${route}/secret-token?q=private`, "https://local.test"), `/${route}/[token]`);
  }
});
