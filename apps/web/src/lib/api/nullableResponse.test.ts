import assert from "node:assert/strict";
import test from "node:test";
import "../../store/chat/moduleResolution.test-helper.mjs";

const { readSuccessResponseData } = await import(new URL("./response.ts", import.meta.url).href);
const { apiTransport } = await import(new URL("./transport.ts", import.meta.url).href);
const { apiFetch } = await import(new URL("./http.ts", import.meta.url).href);
const { queryClient } = await import(new URL("./queryClient.ts", import.meta.url).href);
const { getAgentActiveRun } = await import(new URL("../../features/agent/api/agentApi.ts", import.meta.url).href);

function json(value: unknown, contentType = "application/json") {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "content-type": contentType },
  });
}

function schemaError(error: unknown): boolean {
  return error instanceof Error && "code" in error && error.code === "response_schema_error";
}

function validRun() {
  return {
    id: "run-1", agent_session_id: "session-1",
    user_message_id: "user-1", assistant_message_id: "assistant-1",
    status: "running", execution_epoch: 1, last_event_seq: 3,
    idempotency_key: "message-key-1", model: null, reasoning_effort: null,
    turn_count: 1, tool_call_count: 0, usage: {}, error_code: null,
    error_message: null, started_at: null, finished_at: null,
    cancel_requested_at: null, created_at: "2026-09-13T00:00:00Z",
    updated_at: "2026-09-13T00:00:01Z", references: [], tool_calls: [],
  };
}

test("JSON null is rejected by default and accepted only by explicit contract", async () => {
  await assert.rejects(readSuccessResponseData(json(null)), schemaError);
  await assert.rejects(readSuccessResponseData(json(null), { allowNull: false }), schemaError);
  assert.equal(await readSuccessResponseData(json(null), { allowNull: true }), null);
  assert.equal(await readSuccessResponseData(json(null, "application/problem+json; charset=utf-8"), { allowNull: true }), null);
  assert.equal(await readSuccessResponseData(new Response(null, { status: 204 }), { allowNull: true }), undefined);
});

test("nullable responses still require valid JSON and JSON content type", async () => {
  for (const [body, type, code] of [
    ["{", "application/json", "response_parse_error"],
    ["", "application/json", "response_parse_error"],
    ["null", "text/html", "response_content_type_error"],
  ]) {
    await assert.rejects(
      readSuccessResponseData(new Response(body, { headers: { "content-type": type } }), { allowNull: true }),
      (error: unknown) => error instanceof Error && "code" in error && error.code === code,
    );
  }
});

test("real transport passes nullable values to validators without leaking options into fetch", async (t) => {
  const requests: RequestInit[] = [];
  t.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    requests.push(init);
    return json(null);
  });
  const values: unknown[] = [];
  const validate = (value: unknown) => { values.push(value); return value; };
  assert.equal(await apiTransport.request("/nullable", {
    requestClass: "query", allowNullResponse: true, validate,
  }), null);
  assert.deepEqual(values, [null]);
  assert.equal(requests.length, 1, "successful null must not trigger retries");
  for (const key of ["allowNullResponse", "validate", "requestClass", "expectNoContent", "budget"]) {
    assert.equal(key in requests[0], false, `${key} is not a native fetch option`);
  }
  await assert.rejects(apiTransport.request("/strict", { requestClass: "query", validate }), schemaError);
  assert.deepEqual(values, [null], "strict null rejection must precede validation");
});

test("allowing null does not bypass a rejecting validator", async (t) => {
  t.mock.method(globalThis, "fetch", async () => json(null));
  let called = 0;
  await assert.rejects(apiTransport.request("/nullable", {
    requestClass: "query", allowNullResponse: true,
    validate() { called++; throw new TypeError("object required"); },
  }), schemaError);
  assert.equal(called, 1);
});

test("apiFetch and queryClient preserve the nullable contract end to end", async (t) => {
  const fetchMock = t.mock.method(globalThis, "fetch", async () => json(null));
  assert.equal(await apiFetch("/nullable", { allowNullResponse: true }), null);
  assert.equal(await queryClient.get("/nullable", { allowNullResponse: true }), null);
  await assert.rejects(apiFetch("/strict"), schemaError);
  assert.equal(fetchMock.mock.callCount(), 3);
});

test("Agent active-run returns idle null once and still validates non-null runs", async (t) => {
  let payload: unknown = null;
  const calls: string[] = [];
  t.mock.method(globalThis, "fetch", async (url: string) => {
    calls.push(url);
    return json(payload);
  });
  assert.equal(await getAgentActiveRun("session/2"), null);
  assert.equal(calls.length, 1);
  assert.match(calls[0], /\/agent\/sessions\/session%2F2\/active-run$/u);
  payload = validRun();
  assert.equal((await getAgentActiveRun("session-1")).id, "run-1");
  for (payload of [{}, [], false, 42, "null"]) {
    await assert.rejects(getAgentActiveRun("session-1"), schemaError);
  }
  assert.equal(calls.length, 7);
});
