import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import assert from "node:assert/strict";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(webRoot, "../..");

function source(relativePath) {
  return readFileSync(path.join(repoRoot, relativePath), "utf8");
}

test("HSTS is owned only by the outer nginx template", () => {
  const apiMain = source("apps/api/app/main.py");
  const nextConfig = source("apps/web/next.config.ts");
  const webDockerfile = source("apps/web/Dockerfile");
  const nginx = source("deploy/nginx.conf.example");

  assert.doesNotMatch(apiMain, /strict-transport-security/i);
  assert.doesNotMatch(nextConfig, /Strict-Transport-Security|LUMEN_HSTS/);
  assert.doesNotMatch(webDockerfile, /LUMEN_HSTS/);

  assert.equal(
    (nginx.match(/add_header Strict-Transport-Security/g) ?? []).length,
    1,
  );
  assert.match(nginx, /\$\{LUMEN_HSTS_ENABLED\}/);
  assert.match(nginx, /\$\{LUMEN_HSTS_INCLUDE_SUBDOMAINS\}/);
  assert.match(nginx, /"true:false"\s+"max-age=31536000";/);
  assert.match(
    nginx,
    /"true:true"\s+"max-age=31536000; includeSubDomains";/,
  );
  assert.match(nginx, /default\s+"";/);
});
