import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

type Exports = Record<string, unknown>;
type Snapshot = { type: string; status: string; steps: { status: string }[] };

function compile(path: string, require: (id: string) => unknown): Exports {
  const url = new URL(path, import.meta.url);
  const output = ts.transpileModule(readFileSync(url, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    fileName: url.pathname,
  }).outputText;
  const compiled = { exports: {} as Exports };
  new Function("require", "module", "exports", output)(require, compiled, compiled.exports);
  return compiled.exports;
}

function harness(snapshot: Snapshot, options: {
  afterRead?: () => void; afterWrite?: () => void; writeError?: Error;
} = {}) {
  const calls: { method: string; signal?: AbortSignal; path?: string }[] = [];
  let current = true;
  const getWorkflow = async (_id: string, signal?: AbortSignal) => {
    calls.push({ method: "GET", signal });
    options.afterRead?.();
    return snapshot;
  };
  const result = { ...snapshot, status: "completed" };
  const api = compile("./workflowRefresh.ts", (id) => {
    if (id === "../auth/privateIdentityEpoch") return {
      getPrivateIdentitySnapshot: () => ({ userId: "owner", epoch: 1 }),
      isPrivateIdentitySnapshotCurrent: () => current,
    };
    if (id === "./workflows") return { getWorkflow };
    if (id === "./http") return { apiFetch: async (
      path: string, init: { method: string; signal?: AbortSignal },
    ) => {
      calls.push({ ...init, path });
      options.afterWrite?.();
      if (options.writeError) throw options.writeError;
      return result;
    } };
    throw new Error(`Unexpected dependency: ${id}`);
  });
  return {
    calls, result, getWorkflow,
    refreshWorkflow: api.refreshWorkflow as (
      id: string, signal?: AbortSignal,
    ) => Promise<Snapshot>,
    changeIdentity: () => { current = false; },
  };
}

for (const kind of ["apparel_model_showcase", "poster_design"]) {
  test(`${kind} refresh reconciles running output and forwards cancellation`, async () => {
    const h = harness({ type: kind, status: "running", steps: [] });
    const signal = new AbortController().signal;
    assert.equal(await h.refreshWorkflow("run/one", signal), h.result);
    assert.deepEqual(h.calls.map((call) => call.method), ["GET", "POST"]);
    assert.equal(h.calls[1].path, "/workflows/run%2Fone/reconcile");
    assert.ok(h.calls.every((call) => call.signal === signal));
  });
}

for (const status of ["draft", "completed", "failed"]) {
  test(`${status} workflow with no running steps remains read-only`, async () => {
    const snapshot = { type: "poster_design", status, steps: [] };
    const h = harness(snapshot);
    assert.equal(await h.refreshWorkflow("run"), snapshot);
    assert.deepEqual(h.calls.map((call) => call.method), ["GET"]);
  });
}

test("manual review still syncs late task output without approving it", async () => {
  const h = harness({ type: "poster_design", status: "needs_review", steps: [] });
  await h.refreshWorkflow("run");
  assert.deepEqual(h.calls.map((call) => call.method), ["GET", "POST"]);
});

test("a running step can recover a stale top-level terminal status", async () => {
  const h = harness({ type: "poster_design", status: "completed", steps: [{ status: "running" }] });
  await h.refreshWorkflow("run");
  assert.equal(h.calls.length, 2);
});

test("specialized workflow kinds never enter the apparel/poster reconciler", async () => {
  const h = harness({ type: "storyboard", status: "running", steps: [] });
  await h.refreshWorkflow("run");
  assert.deepEqual(h.calls.map((call) => call.method), ["GET"]);
});

test("cancellation after GET prevents a reconciliation write", async () => {
  const controller = new AbortController();
  const h = harness({ type: "poster_design", status: "running", steps: [] }, {
    afterRead: () => controller.abort(),
  });
  await assert.rejects(h.refreshWorkflow("run", controller.signal), { name: "AbortError" });
  assert.equal(h.calls.length, 1);
});

test("identity changes after GET cannot submit under another account", async () => {
  const h = harness({ type: "poster_design", status: "running", steps: [] }, {
    afterRead: () => h.changeIdentity(),
  });
  await assert.rejects(h.refreshWorkflow("run"), { name: "AbortError" });
  assert.equal(h.calls.length, 1);
});

test("a reconciliation response cannot publish after identity changes", async () => {
  const h = harness({ type: "poster_design", status: "running", steps: [] }, {
    afterWrite: () => h.changeIdentity(),
  });
  await assert.rejects(h.refreshWorkflow("run"), { name: "AbortError" });
  assert.equal(h.calls.length, 2);
});

test("a failed reconciliation is surfaced without immediately replaying POST", async () => {
  const error = new Error("connection lost");
  const h = harness({ type: "poster_design", status: "running", steps: [] }, { writeError: error });
  await assert.rejects(h.refreshWorkflow("run"), error);
  assert.deepEqual(h.calls.map((call) => call.method), ["GET", "POST"]);
});

for (const [path, name] of [
  ["../queries/projects.ts", "useWorkflowQuery"],
  ["../queries/poster.ts", "usePosterWorkflowQuery"],
]) {
  test(`${name} uses the reconciled result rather than polling a stale GET forever`, async () => {
    const h = harness({ type: "poster_design", status: "running", steps: [] });
    const hooks = compile(path, (id) => {
      if (id === "../api/workflows") return { getWorkflow: h.getWorkflow };
      if (id === "../api/workflowRefresh") return { refreshWorkflow: h.refreshWorkflow };
      if (id === "@tanstack/react-query") return { useQuery: (options: unknown) => options };
      if (id === "./privateQueryScope") return {
        useCurrentUserQueryKeys: () => ({ userScope: { enabled: true, userId: "owner" },
          userKeys: { workflow: (key: string) => ["owner", "workflow", key] } }),
        privateQueryEnabled: (...values: unknown[]) => values.every((value) => value !== false),
      };
      if (["react", "./queryKeys", "../api/storyboards", "../api/images",
        "../api/posterWorkflows", "../api/posterStyles"].includes(id)) return {};
      throw new Error(`Unexpected query dependency: ${id}`);
    });
    const useHook = hooks[name] as (id: string) => {
      queryFn: (context: { signal: AbortSignal }) => Promise<Snapshot>; retry: boolean;
    };
    const query = useHook("run");
    const signal = new AbortController().signal;
    assert.equal(await query.queryFn({ signal }), h.result);
    assert.ok(h.calls.every((call) => call.signal === signal));
    assert.equal(query.retry, false, "query retries must not silently replay a command");
  });
}
