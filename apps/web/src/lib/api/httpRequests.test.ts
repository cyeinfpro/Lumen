import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import ts from "typescript";
import "../../store/chat/moduleResolution.test-helper.mjs";

const errors = await import("./errors.ts");
const response = await import("./response.ts");
const budgets = await import("./requestBudget.ts");
const signals = await import("./requestSignal.ts");
const retryPolicy = await import("./retryPolicy.ts");

function compile(path: string, dependencies: Record<string, unknown>): Record<string, unknown> {
  const url = new URL(path, import.meta.url);
  const output = ts.transpileModule(readFileSync(url, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: url.pathname,
  }).outputText;
  const compiledModule = { exports: {} };
  new Function("require", "module", "exports", output)((id: string) => {
    assert.ok(id in dependencies, `missing dependency ${id}`);
    return dependencies[id];
  }, compiledModule, compiledModule.exports);
  return compiledModule.exports;
}

test("compatibility HEAD uses real query transport without parsing a success body and retains auth coordination", async () => {
  const calls: RequestInit[] = [];
  let unauthorized = 0;
  let mismatches = 0;
  let checkedIdentity = 0;
  let stale = false;
  let next = new Response(null, { status: 200 });
  const coordinator = { coordinateUnauthorized: () => { unauthorized += 1; } };
  const csrf = { csrfService: {} };
  const transport = compile("./transport.ts", {
    "@/lib/auth/authFailureCoordinator": coordinator,
    "@/lib/auth/identityPolicy": {
      applyConfirmedIdentityHeader: (headers: Headers) => { headers.set("x-lumen-user", "owner"); return "owner"; },
      assertConfirmedIdentityResponse: () => {
        checkedIdentity += 1;
        if (stale) throw new errors.ApiError({ status: 409, code: "identity_mismatch", message: "stale" });
      },
      coordinateIdentityMismatchResponse: (status: number) => { if (status === 409) mismatches += 1; },
    },
    "./baseUrl": { apiUrl: (path: string) => `/api${path}` },
    "./csrf": csrf, "./errors": errors, "./requestBudget": budgets,
    "./requestSignal": signals, "./response": response, "./retryPolicy": retryPolicy,
    "./fetchExecutor": { executeFetch: async (_url: string, request: () => RequestInit) => {
      calls.push(request()); return next;
    } },
  });
  const query = compile("./queryClient.ts", { "./transport": transport });
  const http = compile("./http.ts", {
    "@/lib/auth/privateStateCleanup": {}, "@/lib/auth/authFailureCoordinator": coordinator,
    "@/lib/auth/navigation": {}, "./baseUrl": {}, "./commandClient": {}, "./csrf": csrf,
    "./errors": errors, "./queryClient": query, "./requestBudget": budgets, "./uploadClient": {},
  }) as { apiFetch: typeof import("./http.ts").apiFetch };
  next.json = async () => assert.fail("HEAD success cannot read JSON");
  next.text = async () => assert.fail("HEAD success cannot read text");
  const signal = new AbortController().signal;
  const headResult: Promise<undefined> = http.apiFetch("/probe", { method: "HEAD", signal, timeoutMs: 1234, validate: () => assert.fail("no-body validator") });
  assert.equal(await headResult, undefined);
  assert.equal(calls[0].method, "HEAD");
  assert.equal(calls[0].credentials, "include");
  assert.equal(new Headers(calls[0].headers).get("x-lumen-user"), "owner");
  assert.equal(checkedIdentity, 1);
  next = new Response('{"ok":true}', { headers: { "content-type": "application/json" } });
  assert.deepEqual(await http.apiFetch("/probe"), { ok: true });
  assert.equal(calls[1].method, "GET");
  next = new Response(null, { status: 401 });
  await assert.rejects(http.apiFetch("/probe", { method: "HEAD" }), { code: "unauthorized" });
  assert.equal(unauthorized, 1);
  next = new Response('{"code":"identity_mismatch"}', { status: 409, headers: { "content-type": "application/json" } });
  await assert.rejects(http.apiFetch("/probe", { method: "HEAD" }), { status: 409 });
  assert.equal(mismatches, 1);
  next = new Response(null, { status: 200 });
  stale = true;
  await assert.rejects(http.apiFetch("/probe", { method: "HEAD" }), { code: "identity_mismatch" });
  assert.ok(calls.slice(2).every((call) => call.method === "HEAD"));
});
