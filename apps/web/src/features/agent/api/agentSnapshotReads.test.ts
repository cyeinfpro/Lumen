import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
import type { AgentSnapshotReads } from "./agentSnapshotReads";
import type { AgentMessageList } from "../model/contracts";

function snapshot(cursor: string | null = null): AgentMessageList {
  return {
    items: [], runs: [], next_cursor: cursor,
    generations: [], completions: [], images: [],
  };
}

type Call = {
  kind: string; sessionId: string; signal: AbortSignal;
  resolve: (value: unknown) => void; reject: (error: Error) => void;
};

function harness() {
  let identity = { userId: "owner", epoch: 1 };
  const calls: Call[] = [];
  const load = (kind: string, sessionId: string, signal: AbortSignal) =>
    new Promise((resolve, reject) => calls.push({ kind, sessionId, signal, resolve, reject }));
  const source = readFileSync(new URL("./agentSnapshotReads.ts", import.meta.url), "utf8");
  const output = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  const compiled = { exports: {} as { AgentSnapshotReads: new () => AgentSnapshotReads } };
  new Function("require", "module", "exports", output)((id: string) => {
    if (id === "@/lib/auth/privateIdentityEpoch") return {
      getPrivateIdentitySnapshot: () => identity,
      isPrivateIdentitySnapshotCurrent: (value: typeof identity) =>
        value.userId === identity.userId && value.epoch === identity.epoch,
    };
    if (id === "./agentApi") return {
      listAgentMessages: (sessionId: string, options: { signal: AbortSignal }) =>
        load("messages", sessionId, options.signal),
      getAgentActiveRun: (sessionId: string, signal: AbortSignal) =>
        load("active-run", sessionId, signal),
    };
    throw new Error(`Unexpected dependency: ${id}`);
  }, compiled, compiled.exports);
  return {
    reads: new compiled.exports.AgentSnapshotReads(), calls,
    changeIdentity: (next: typeof identity) => { identity = next; },
  };
}
const settle = () => new Promise<void>((resolve) => setImmediate(resolve));

test("query refetch and SSE/polling share one message read with separate observer signals", async () => {
  const h = harness();
  const query = new AbortController();
  const realtime = new AbortController();
  const first = h.reads.messages("session", { limit: 100, signal: query.signal });
  const second = h.reads.messages("session", { limit: 100, includeTasks: true, signal: realtime.signal });
  await settle();
  assert.equal(h.calls.length, 1);
  assert.notEqual(h.calls[0].signal, query.signal);
  const result = snapshot();
  h.calls[0].resolve(result);
  const responses = await Promise.all([first, second]);
  assert.ok(responses.every((response) => response === result));
});

test("nullable active-run queries share a read without sharing message requests", async () => {
  const h = harness();
  const pending = [h.reads.activeRun("session"), h.reads.activeRun("session"), h.reads.messages("session")];
  await settle();
  assert.equal(h.calls.length, 2);
  h.calls[0].resolve(null);
  h.calls[1].resolve(snapshot());
  const results = await Promise.all(pending);
  assert.equal(results[0], null);
  assert.equal(results[1], null);
});

test("one cancelled observer cannot abort a live observer's snapshot", async () => {
  const h = harness();
  const controller = new AbortController();
  const cancelled = h.reads.messages("session", { signal: controller.signal });
  const survivor = h.reads.messages("session");
  const rejected = assert.rejects(cancelled, { name: "AbortError" });
  await settle();
  controller.abort();
  await rejected;
  assert.equal(h.calls[0].signal.aborted, false);
  h.calls[0].resolve(snapshot());
  assert.deepEqual(await survivor, snapshot());
});

test("last-observer cancellation aborts the network; late completion cannot evict its replacement", async () => {
  const h = harness();
  const controller = new AbortController();
  const old = h.reads.messages("session", { signal: controller.signal });
  const rejected = assert.rejects(old, { name: "AbortError" });
  await settle();
  controller.abort();
  await rejected;
  assert.equal(h.calls[0].signal.aborted, true);
  const replacement = h.reads.messages("session");
  await settle();
  assert.equal(h.calls.length, 2);
  h.calls[0].resolve(snapshot("stale"));
  await settle();
  const joined = h.reads.messages("session");
  await settle();
  assert.equal(h.calls.length, 2);
  h.calls[1].resolve(snapshot("fresh"));
  assert.deepEqual(await Promise.all([replacement, joined]), [snapshot("fresh"), snapshot("fresh")]);
});

test("pre-aborted requests do not launch a network read", async () => {
  const h = harness();
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(h.reads.activeRun("session", controller.signal), { name: "AbortError" });
  assert.equal(h.calls.length, 0);
});

test("cancelling every observer before dispatch does not send the queued request", async () => {
  const h = harness();
  const controller = new AbortController();
  const pending = h.reads.activeRun("session", controller.signal);
  const rejected = assert.rejects(pending, { name: "AbortError" });
  controller.abort();
  await rejected;
  await settle();
  assert.equal(h.calls.length, 0);
});

test("finished reads are not a stale response cache", async () => {
  const h = harness();
  const first = h.reads.activeRun("session");
  await settle();
  h.calls[0].resolve(null);
  await first;
  const second = h.reads.activeRun("session");
  await settle();
  assert.equal(h.calls.length, 2);
  h.calls[1].resolve({ id: "new-run" });
  assert.deepEqual(await second, { id: "new-run" });
});

test("failed shared reads reject all observers and leave retries available", async () => {
  const h = harness();
  const first = h.reads.messages("session");
  const second = h.reads.messages("session");
  const failures = [assert.rejects(first, /offline/), assert.rejects(second, /offline/)];
  await settle();
  h.calls[0].reject(new Error("offline"));
  await Promise.all(failures);
  const retry = h.reads.messages("session");
  await settle();
  assert.equal(h.calls.length, 2);
  h.calls[1].resolve(snapshot());
  await retry;
});

test("sessions, pagination and task inclusion never coalesce", async () => {
  const h = harness();
  const pending = [
    h.reads.messages("one", { limit: 100 }),
    h.reads.messages("two", { limit: 100 }),
    h.reads.messages("one", { limit: 50 }),
    h.reads.messages("one", { limit: 100, cursor: "older" }),
    h.reads.messages("one", { limit: 100, since: "newer" }),
    h.reads.messages("one", { limit: 100, includeTasks: false }),
  ];
  await settle();
  assert.equal(h.calls.length, pending.length);
  for (const call of h.calls) call.resolve(snapshot());
  await Promise.all(pending);
});


test("identity changes before deferred dispatch cannot issue requests for the previous account", async () => {
  const h = harness();
  const pending = h.reads.messages("session");
  h.changeIdentity({ userId: "other", epoch: 2 });
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(h.calls.length, 0);
});

test("in-flight account and epoch changes neither share nor publish stale private results", async () => {
  const h = harness();
  const old = h.reads.messages("session");
  const oldRejected = assert.rejects(old, { name: "AbortError" });
  await settle();
  h.changeIdentity({ userId: "other", epoch: 1 });
  const other = h.reads.messages("session");
  const otherRejected = assert.rejects(other, { name: "AbortError" });
  await settle();
  h.changeIdentity({ userId: "owner", epoch: 2 });
  const current = h.reads.messages("session");
  await settle();
  assert.equal(h.calls.length, 3);
  for (const call of h.calls) call.resolve(snapshot());
  await Promise.all([oldRejected, otherRejected]);
  assert.deepEqual(await current, snapshot());
});
