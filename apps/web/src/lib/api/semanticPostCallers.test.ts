import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";
import "../../store/chat/moduleResolution.test-helper.mjs";

type ModuleExports = Record<string, unknown>;

type RecordedCall = {
  path: string;
  key: string;
  body: Record<string, unknown>;
};

const semanticIdempotencyPersistence = await import(
  "./semanticIdempotencyPersistence.ts"
);
const semanticIdempotencyLease = await import(
  "./semanticIdempotencyLease.ts"
);
const semanticIdempotencyJournal = await import(
  "./semanticIdempotencyJournal.ts"
);
const semanticIdempotencyMigration = await import(
  "./semanticIdempotencyMigration.ts"
);
const semanticIdempotencyRequest = await import(
  "./semanticIdempotencyRequest.ts"
);
const semanticIdempotencyStorage = await import(
  "./semanticIdempotencyStorage.ts"
);
const semanticIdempotencyStorageLock = await import(
  "./semanticIdempotencyStorageLock.ts"
);
const semanticIdempotencySemantics = await import(
  "./semanticIdempotencySemantics.ts"
);
const semanticIdempotencyRecords = await import(
  "./semanticIdempotencyRecords.ts"
);

function compile(
  relativePath: string,
  overrides: Record<string, unknown>,
): ModuleExports {
  const url = new URL(relativePath, import.meta.url);
  const output = ts.transpileModule(readFileSync(url, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
    fileName: url.pathname,
  }).outputText;
  const compiledModule = { exports: {} as ModuleExports };
  new Function("require", "module", "exports", output)(
    (id: string) => {
      if (id in overrides) return overrides[id];
      throw new Error(`missing test dependency: ${id}`);
    },
    compiledModule,
    compiledModule.exports,
  );
  return compiledModule.exports;
}

function loadSemanticModule(keyPrefix = "semantic-key"): ModuleExports {
  let sequence = 0;
  return compile("./semanticIdempotency.ts", {
    "../utils": {
      uuid: () => `${keyPrefix}-${++sequence}`,
    },
    "./semanticIdempotencyJournal": semanticIdempotencyJournal,
    "./semanticIdempotencyLease": semanticIdempotencyLease,
    "./semanticIdempotencyMigration": semanticIdempotencyMigration,
    "./semanticIdempotencyPersistence": semanticIdempotencyPersistence,
    "./semanticIdempotencyRecords": semanticIdempotencyRecords,
    "./semanticIdempotencyRequest": semanticIdempotencyRequest,
    "./semanticIdempotencySemantics": semanticIdempotencySemantics,
    "./semanticIdempotencyStorage": semanticIdempotencyStorage,
    "./semanticIdempotencyStorageLock": semanticIdempotencyStorageLock,
  });
}

function responseLossHarness(
  responseForPath: (path: string) => unknown,
  firstFailure: (
    path: string,
    body: Record<string, unknown>,
  ) => unknown = () =>
    new TypeError("response lost after backend accepted request"),
): {
  accepted: Map<string, unknown>;
  calls: RecordedCall[];
  request(path: string, init?: RequestInit): Promise<unknown>;
} {
  const accepted = new Map<string, unknown>();
  const calls: RecordedCall[] = [];
  return {
    accepted,
    calls,
    async request(path: string, init: RequestInit = {}) {
      const key = new Headers(init.headers).get("Idempotency-Key");
      assert.ok(key, `missing idempotency header for ${path}`);
      const body = init.body
        ? (JSON.parse(String(init.body)) as Record<string, unknown>)
        : {};
      calls.push({ path, key, body });
      const replay = accepted.get(key);
      if (replay !== undefined) return replay;
      const response = responseForPath(path);
      accepted.set(key, response);
      throw firstFailure(path, body);
    },
  };
}

class MemoryStorage {
  private readonly values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  key(index: number): string | null {
    return [...this.values.keys()][index] ?? null;
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

const { createAgentDraft } = await import("../../features/agent/model/contracts.ts");
const { agentMessagePayload } = await import("../../features/agent/containers/agentSubmission.ts");

async function loadAgentRequests(storage: MemoryStorage, request: (path: string, init?: RequestInit) => Promise<unknown>) {
  const semantic = loadSemanticModule();
  const Store = semantic.SemanticIdempotencyStore as typeof import("./semanticIdempotency.ts").SemanticIdempotencyStore;
  const store = new Store({ storage, lockRequest: null });
  semantic.semanticPostIdempotency = store;
  const identity = await import("../auth/privateIdentityEpoch.ts");
  const errors = await import("./errors.ts");
  const validators = await import("../../features/agent/api/validators.ts");
  const api = compile("../../features/agent/api/agentApi.ts", {
    "@/lib/api/http": { apiFetch: request }, "@/lib/api/semanticIdempotency": semantic,
    "./validators": validators,
  });
  const logical = compile("../../features/agent/api/logicalAgentRequests.ts", {
    "@/lib/api/errors": errors, "@/lib/api/semanticIdempotency": semantic,
    "@/lib/auth/privateIdentityEpoch": identity, "./agentApi": api,
  }) as unknown as typeof import("../../features/agent/api/logicalAgentRequests.ts");
  return { logical, store, identity, errors };
}

test("Agent 504 retries survive reload and tabs, isolate payload/session/source-run, and advance only after confirmation", async () => {
  const storage = new MemoryStorage();
  const harness = responseLossHarness(() => ({ id: "accepted-run" }), () => ({ status: 504 }));
  const first = await loadAgentRequests(storage, harness.request);
  first.identity.transitionPrivateIdentity("agent-owner");
  await first.store.activateIdentity("agent-owner");
  const attempts: string[] = [];
  const input = {
    userId: "agent-owner", sessionId: "session-1",
    payload: agentMessagePayload({ ...createAgentDraft(), text: "same text" }, true),
    onAttempt: (key: string) => attempts.push(key),
  };
  await assert.rejects(first.logical.submitLogicalAgentMessage(input));
  assert.equal(harness.calls.length, 1, "504 is uncertain but not an immediate transport retry");
  const tab = await loadAgentRequests(storage, harness.request);
  await tab.store.activateIdentity("agent-owner");
  await tab.logical.submitLogicalAgentMessage(input);
  assertReplayPair(harness.calls, 0, { bodyKey: true });
  assert.equal(attempts[0], attempts[1]);
  await assert.rejects(first.logical.submitLogicalAgentMessage(input));
  assert.notEqual(harness.calls[2].key, harness.calls[0].key, "explicit repeat after confirmation is new intent");
  await assert.rejects(first.logical.submitLogicalAgentMessage({ ...input, sessionId: "session-2" }));
  await assert.rejects(first.logical.submitLogicalAgentMessage({ ...input, payload: { ...input.payload, text: "edited draft" } }));
  assert.equal(new Set(harness.calls.slice(2).map((call) => call.key)).size, 3);

  const continuation = { userId: "agent-owner", sessionId: "session-1", runId: "source-run" };
  await assert.rejects(first.logical.continueLogicalAgentRun(continuation));
  await tab.logical.continueLogicalAgentRun(continuation);
  assertReplayPair(harness.calls, 5, { bodyKey: true });
  await assert.rejects(tab.logical.continueLogicalAgentRun({ ...continuation, runId: "other-source" }));
  assert.notEqual(harness.calls[7].key, harness.calls[5].key);
  assert.equal(harness.accepted.size, 6);
});

test("Agent immediate network retries share a key, definitive rejections retire it, and stale replies cannot retry under another identity", async () => {
  const storage = new MemoryStorage();
  const keys: string[] = [];
  let mode: "network" | "reject" | "stale" = "network";
  let call = 0;
  const agent = await loadAgentRequests(storage, async (_path, init) => {
    keys.push(new Headers(init?.headers).get("Idempotency-Key")!);
    if (mode === "stale") {
      agent.identity.transitionPrivateIdentity("other-owner");
      await agent.store.activateIdentity("other-owner");
      throw new agent.errors.ApiError({ status: 0, code: "network_error", message: "lost" });
    }
    if (mode === "reject") throw new agent.errors.ApiError({ status: 422, code: "invalid", message: "rejected" });
    if (++call === 1) throw new agent.errors.ApiError({ status: 0, code: "network_error", message: "lost" });
    return { id: "accepted" };
  });
  agent.identity.transitionPrivateIdentity("agent-owner");
  await agent.store.activateIdentity("agent-owner");
  const input = { userId: "agent-owner", sessionId: "session", payload: agentMessagePayload({ ...createAgentDraft(), text: "same" }, true), onAttempt: () => {} };
  await agent.logical.submitLogicalAgentMessage(input);
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);
  mode = "reject";
  await assert.rejects(agent.logical.submitLogicalAgentMessage(input), { status: 422 });
  await assert.rejects(agent.logical.submitLogicalAgentMessage(input), { status: 422 });
  assert.notEqual(keys[2], keys[3]);
  mode = "stale";
  await assert.rejects(agent.logical.submitLogicalAgentMessage(input), { code: "identity_changed" });
  assert.equal(keys.length, 5, "stale identity cannot trigger the immediate second POST");
  agent.identity.transitionPrivateIdentity("agent-owner");
  await agent.store.activateIdentity("agent-owner");
  mode = "network";
  await agent.logical.submitLogicalAgentMessage(input);
  assert.equal(keys[5], keys[4], "uncertain operation survives relogin without new identity pollution");
});

test("only a matching verified Agent snapshot retires a lost submission, including after reload", async () => {
  const storage = new MemoryStorage();
  const harness = responseLossHarness(() => ({}), () => ({ status: 504 }));
  const agent = await loadAgentRequests(storage, harness.request);
  const identity = agent.identity.transitionPrivateIdentity("snapshot-owner");
  await agent.store.activateIdentity("snapshot-owner");
  const input = { userId: "snapshot-owner", sessionId: "snapshot-session", payload: agentMessagePayload({ ...createAgentDraft(), text: "lost" }, true), onAttempt: () => {} };
  await assert.rejects(agent.logical.submitLogicalAgentMessage(input));
  const key = harness.calls[0].key;
  const reloaded = await loadAgentRequests(storage, harness.request);
  await reloaded.store.activateIdentity("snapshot-owner");
  const run = { id: "verified-run", agent_session_id: input.sessionId, idempotency_key: key } as import("../../features/agent/model/contracts.ts").AgentRun;
  await reloaded.logical.confirmObservedAgentRuns(identity, "other-session", [run]);
  await reloaded.logical.confirmObservedAgentRuns(identity, input.sessionId, [{ ...run, id: "optimistic:local" }]);
  const scope = { operation: "agent.message.create", userId: input.userId, sessionId: input.sessionId };
  assert.equal((await reloaded.store.acquire(scope, input.payload)).key, key);
  await reloaded.logical.confirmObservedAgentRuns(identity, input.sessionId, [run]);
  const next = await reloaded.store.acquire(scope, input.payload);
  assert.notEqual(next.key, key);
  agent.identity.transitionPrivateIdentity("another-owner");
  await assert.rejects(reloaded.logical.confirmObservedAgentRuns(identity, input.sessionId, [{ ...run, idempotency_key: next.key }]), { code: "identity_changed" });
  assert.equal((await reloaded.store.acquire(scope, input.payload)).key, next.key);
});

function assertReplayPair(
  calls: RecordedCall[],
  start: number,
  options: { bodyKey: boolean },
): void {
  const first = calls[start];
  const retry = calls[start + 1];
  assert.ok(first);
  assert.ok(retry);
  assert.equal(retry.key, first.key);
  if (options.bodyKey) {
    assert.equal(first.body.idempotency_key, first.key);
    assert.equal(retry.body.idempotency_key, retry.key);
  }
}

function canvasDocument(id: string, title: string): Record<string, unknown> {
  return {
    id,
    title,
    description: "",
    revision: 1,
    graph_schema_version: 1,
    graph: {
      schema_version: 1,
      nodes: [],
      edges: [],
      frames: [],
      settings: { snap_to_grid: false, grid_size: 16 },
    },
  };
}

function workflowTaskRun(
  stepKey: string,
  options: {
    activeTaskIds?: string[];
    activeRenderId?: string;
  } = {},
): Record<string, unknown> {
  const taskIds = options.activeTaskIds ?? ["task-1"];
  return {
    id: "workflow-1",
    steps: [
      {
        step_key: stepKey,
        task_ids: taskIds,
        input_json: {
          active_task_ids: taskIds,
          ...(options.activeRenderId
            ? { active_render_id: options.activeRenderId }
            : {}),
        },
      },
    ],
  };
}

function storyboardTaskRun(options: {
  assetId?: string;
  assetGenerationId?: string;
  shotId?: string;
  shotStatus?: string;
  keyframeGenerationId?: string;
  videoGenerationId?: string;
}): Record<string, unknown> {
  return {
    id: "storyboard-1",
    assets: options.assetId
      ? [
          {
            id: options.assetId,
            generation_id: options.assetGenerationId,
          },
        ]
      : [],
    shots: options.shotId
      ? [
          {
            id: options.shotId,
            status: options.shotStatus ?? "approved",
            keyframe_generation_id: options.keyframeGenerationId,
            video_generation_id: options.videoGenerationId,
          },
        ]
      : [],
  };
}

function paidApparelResponse(path: string): unknown | undefined {
  if (path === "/workflows/apparel-model-showcase") {
    return {
      workflow_run_id: "workflow-apparel",
      status: "running",
      current_step: "product_analysis",
    };
  }
  if (path.endsWith("/model-candidates/accessory-previews")) {
    return workflowTaskRun("model_approval");
  }
  if (path.endsWith("/model-candidates")) {
    return workflowTaskRun("model_candidates");
  }
  if (path === "/workflows/apparel-model-library/generate") {
    return {
      job_id: "job-1",
      workflow_run_id: "workflow-library",
    };
  }
  if (
    path.includes("/apparel-model-library/items/") &&
    path.endsWith("/auto-tag")
  ) {
    return { item_id: "library-item-1" };
  }
  if (
    path === "/workflows/apparel-model-library/items" ||
    path.endsWith("/save-to-library") ||
    path.includes("/apparel-model-library/jobs/")
  ) {
    return { id: "library-item-1" };
  }
  if (
    path.endsWith("/showcase-images") ||
    path.endsWith("/images/image-1/revise")
  ) {
    return workflowTaskRun("showcase_generation");
  }
  return undefined;
}

function paidPosterResponse(path: string): unknown | undefined {
  if (path === "/workflows/poster-design") {
    return {
      workflow_run_id: "workflow-poster",
      status: "running",
      current_step: "copy_analysis",
    };
  }
  if (path.endsWith("/masters")) {
    return workflowTaskRun("master_generation");
  }
  if (path.endsWith("/renders")) {
    return workflowTaskRun("multi_size_generation");
  }
  if (path.endsWith("/renders/render-1/revise")) {
    return workflowTaskRun("multi_size_generation", {
      activeRenderId: "render-1",
    });
  }
  if (path.endsWith("/renders/render-1/inpaint")) {
    return workflowTaskRun("multi_size_generation", {
      activeRenderId: "render-1",
    });
  }
  return undefined;
}

function paidStoryboardVideoResponse(path: string): unknown | undefined {
  if (path.endsWith("/assets/asset-1/generate")) {
    return storyboardTaskRun({
      assetId: "asset-1",
      assetGenerationId: "generation-asset",
    });
  }
  if (path.endsWith("/shots/shot-1/keyframe")) {
    return storyboardTaskRun({
      shotId: "shot-1",
      shotStatus: "keyframe_generating",
      keyframeGenerationId: "generation-keyframe",
    });
  }
  if (path.endsWith("/shots/keyframes/generate-all")) {
    return storyboardTaskRun({
      shotId: "shot-batch",
      shotStatus: "keyframe_generating",
      keyframeGenerationId: "generation-keyframe-batch",
    });
  }
  if (path.endsWith("/shots/shot-2/submit")) {
    return storyboardTaskRun({
      shotId: "shot-2",
      shotStatus: "generating",
      videoGenerationId: "video-shot",
    });
  }
  if (path.endsWith("/shots/submit-all")) {
    return storyboardTaskRun({
      shotId: "shot-batch",
      shotStatus: "generating",
      videoGenerationId: "video-batch",
    });
  }
  if (path.endsWith("/videos/generations/video-old/retry")) {
    return { id: "video-retry" };
  }
  return undefined;
}

function paidCallerResponse(path: string): unknown {
  const apparel = paidApparelResponse(path);
  if (apparel !== undefined) return apparel;
  const poster = paidPosterResponse(path);
  if (poster !== undefined) return poster;
  const storyboardVideo = paidStoryboardVideoResponse(path);
  if (storyboardVideo !== undefined) return storyboardVideo;
  throw new Error(`unexpected paid POST path: ${path}`);
}

test("canvas create, duplicate, and execute reuse accepted keys after response loss", async () => {
  const semantic = loadSemanticModule();
  const harness = responseLossHarness((path) => {
    if (path.endsWith("/execute")) {
      return {
        run: { id: "run-1", status: "queued", target_node_ids: ["node-1"] },
        execution: { id: "execution-1", node_id: "node-1", status: "queued" },
      };
    }
    if (path.endsWith("/duplicate")) {
      return canvasDocument("canvas-copy", "Copy");
    }
    return canvasDocument("canvas-created", "Created");
  });
  const canvases = compile("./canvases.ts", {
    "./http": {
      apiFetch: harness.request,
      apiFetchNoContent: async () => undefined,
    },
    "./semanticIdempotency": semantic,
    "../canvas/graph": {
      normalizeCanvasGraph: (value: unknown) => value,
    },
  }) as {
    createCanvas(input: Record<string, unknown>): Promise<{ id: string }>;
    duplicateCanvas(canvasId: string): Promise<{ id: string }>;
    executeCanvasNode(
      canvasId: string,
      nodeId: string,
      revision: number,
    ): Promise<{ run?: { id: string } }>;
  };

  const createInput = { title: "Created" };
  await assert.rejects(
    canvases.createCanvas(createInput),
    /response lost/,
  );
  assert.equal((await canvases.createCanvas(createInput)).id, "canvas-created");

  await assert.rejects(
    canvases.duplicateCanvas("canvas-source"),
    /response lost/,
  );
  assert.equal(
    (await canvases.duplicateCanvas("canvas-source")).id,
    "canvas-copy",
  );

  await assert.rejects(
    canvases.executeCanvasNode("canvas-source", "node-1", 7),
    /response lost/,
  );
  assert.equal(
    (await canvases.executeCanvasNode("canvas-source", "node-1", 7)).run?.id,
    "run-1",
  );

  assert.equal(harness.accepted.size, 3);
  assertReplayPair(harness.calls, 0, { bodyKey: true });
  assertReplayPair(harness.calls, 2, { bodyKey: true });
  assertReplayPair(harness.calls, 4, { bodyKey: true });
});

test("canvas execution parameter changes acquire a new semantic key", async () => {
  const semantic = loadSemanticModule();
  const harness = responseLossHarness(() => ({
    run: { id: "run-1", status: "queued", target_node_ids: ["node-1"] },
  }));
  const canvases = compile("./canvases.ts", {
    "./http": {
      apiFetch: harness.request,
      apiFetchNoContent: async () => undefined,
    },
    "./semanticIdempotency": semantic,
    "../canvas/graph": {
      normalizeCanvasGraph: (value: unknown) => value,
    },
  }) as {
    executeCanvasNode(
      canvasId: string,
      nodeId: string,
      revision: number,
    ): Promise<unknown>;
  };

  await assert.rejects(
    canvases.executeCanvasNode("canvas-1", "node-1", 7),
    /response lost/,
  );
  await assert.rejects(
    canvases.executeCanvasNode("canvas-1", "node-1", 8),
    /response lost/,
  );

  assert.notEqual(harness.calls[0]?.key, harness.calls[1]?.key);
});

test("redemption, code batches, and wallet adjustments replay accepted keys", async () => {
  const semantic = loadSemanticModule();
  const harness = responseLossHarness((path) => {
    if (path === "/me/redemptions") {
      return {
        amount: { micro: 500_000, rmb: "0.5" },
        balance: { micro: 1_500_000, rmb: "1.5" },
      };
    }
    if (path === "/admin/redemption_codes") {
      return {
        batch_id: "batch-1",
        count: 2,
        amount: { micro: 500_000, rmb: "0.5" },
        download_token: "token-1",
        plaintext_codes: ["A", "B"],
      };
    }
    return {
      id: "tx-1",
      kind: "adjust_admin",
      amount: { micro: 500_000, rmb: "0.5" },
    };
  });
  const billing = compile("./billing.ts", {
    "./http": {
      API_BASE: "/api",
      apiFetch: harness.request,
    },
    "./semanticIdempotency": semantic,
  }) as {
    redeemCode(code: string): Promise<unknown>;
    createAdminRedemptionCodes(
      body: Record<string, unknown>,
    ): Promise<unknown>;
    adjustAdminWallet(
      userId: string,
      amount: string,
      reason: string,
    ): Promise<unknown>;
  };

  await assert.rejects(billing.redeemCode("CODE-1"), /response lost/);
  await billing.redeemCode("CODE-1");

  const batch = { amount_rmb: "0.5", count: 2 };
  await assert.rejects(
    billing.createAdminRedemptionCodes(batch),
    /response lost/,
  );
  await billing.createAdminRedemptionCodes(batch);

  await assert.rejects(
    billing.adjustAdminWallet("user-1", "0.5", "manual credit"),
    /response lost/,
  );
  await billing.adjustAdminWallet("user-1", "0.5", "manual credit");

  assert.equal(harness.accepted.size, 3);
  assertReplayPair(harness.calls, 0, { bodyKey: false });
  assertReplayPair(harness.calls, 2, { bodyKey: false });
  assertReplayPair(harness.calls, 4, { bodyKey: true });
});

test("malformed 2xx payloads retain semantic keys until caller validation succeeds", async () => {
  const semantic = loadSemanticModule();
  const canvasHarness = responseLossHarness(() => ({}));
  const canvases = compile("./canvases.ts", {
    "./http": {
      apiFetch: canvasHarness.request,
      apiFetchNoContent: async () => undefined,
    },
    "./semanticIdempotency": semantic,
    "../canvas/graph": {
      normalizeCanvasGraph: (value: unknown) => value,
    },
  }) as {
    createCanvas(input: Record<string, unknown>): Promise<unknown>;
  };

  await assert.rejects(
    canvases.createCanvas({ title: "Malformed" }),
    /response lost/,
  );
  await assert.rejects(
    canvases.createCanvas({ title: "Malformed" }),
    /malformed canvas document response/,
  );
  await assert.rejects(
    canvases.createCanvas({ title: "Malformed" }),
    /malformed canvas document response/,
  );
  assert.equal(canvasHarness.calls[1]?.key, canvasHarness.calls[0]?.key);
  assert.equal(canvasHarness.calls[2]?.key, canvasHarness.calls[0]?.key);

  const billingHarness = responseLossHarness(() => ({}));
  const billing = compile("./billing.ts", {
    "./http": {
      API_BASE: "/api",
      apiFetch: billingHarness.request,
    },
    "./semanticIdempotency": semantic,
  }) as {
    adjustAdminWallet(
      userId: string,
      amount: string,
      reason: string,
    ): Promise<unknown>;
  };

  await assert.rejects(
    billing.adjustAdminWallet("user-1", "0.5", "malformed"),
    /response lost/,
  );
  await assert.rejects(
    billing.adjustAdminWallet("user-1", "0.5", "malformed"),
    /malformed wallet transaction response/,
  );
  assert.equal(billingHarness.calls[1]?.key, billingHarness.calls[0]?.key);
});

test("video creation survives network loss, 5xx, and reload with the original key", async () => {
  const originalStorage = Object.getOwnPropertyDescriptor(
    globalThis,
    "localStorage",
  );
  const originalNavigator = Object.getOwnPropertyDescriptor(
    globalThis,
    "navigator",
  );
  const storage = new MemoryStorage();
  let lockTail: Promise<void> = Promise.resolve();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: storage,
  });
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: {
      locks: {
        async request<T>(
          _name: string,
          _options: { mode: "exclusive" },
          callback: () => T | Promise<T>,
        ): Promise<T> {
          const prior = lockTail;
          let release!: () => void;
          lockTail = new Promise<void>((resolve) => {
            release = resolve;
          });
          await prior;
          try {
            return await callback();
          } finally {
            release();
          }
        },
      },
    },
  });
  try {
    const harness = responseLossHarness(
      () => ({ id: "video-generation-1" }),
      (_path, body) =>
        body.prompt === "network loss"
          ? new TypeError("network response lost")
          : { status: 503 },
    );
    const firstSemantic = loadSemanticModule("first-load-key");
    const firstStore = firstSemantic.semanticPostIdempotency as {
      activateIdentity(userId: string): Promise<void>;
    };
    await firstStore.activateIdentity("user-1");
    const firstVideo = compile("./videoGenerations.ts", {
      "./http": { apiFetch: harness.request },
      "./semanticIdempotency": firstSemantic,
    }) as {
      createVideoGeneration(
        body: Record<string, unknown>,
      ): Promise<{ id: string }>;
    };
    const fiveHundredPayload = {
      action: "t2v",
      model: "video-model",
      prompt: "503 accepted",
      duration_s: 5,
      resolution: "1080p",
      aspect_ratio: "16:9",
      generate_audio: false,
      watermark: false,
    };

    await assert.rejects(firstVideo.createVideoGeneration(fiveHundredPayload));

    const reloadedSemantic = loadSemanticModule("reload-key");
    const reloadedStore = reloadedSemantic.semanticPostIdempotency as {
      activateIdentity(userId: string): Promise<void>;
    };
    await reloadedStore.activateIdentity("user-1");
    const reloadedVideo = compile("./videoGenerations.ts", {
      "./http": { apiFetch: harness.request },
      "./semanticIdempotency": reloadedSemantic,
    }) as {
      createVideoGeneration(
        body: Record<string, unknown>,
      ): Promise<{ id: string }>;
    };

    assert.equal(
      (await reloadedVideo.createVideoGeneration(fiveHundredPayload)).id,
      "video-generation-1",
    );
    assertReplayPair(harness.calls, 0, { bodyKey: true });
    assert.match(harness.calls[0]?.key ?? "", /^[a-f0-9]{64}$/);
    assert.equal(harness.calls[0]?.key.length, 64);

    const networkPayload = {
      ...fiveHundredPayload,
      prompt: "network loss",
    };
    await assert.rejects(
      reloadedVideo.createVideoGeneration(networkPayload),
      /network response lost/,
    );
    await reloadedVideo.createVideoGeneration(networkPayload);
    assertReplayPair(harness.calls, 2, { bodyKey: true });
  } finally {
    if (originalStorage) {
      Object.defineProperty(globalThis, "localStorage", originalStorage);
    } else {
      Reflect.deleteProperty(globalThis, "localStorage");
    }
    if (originalNavigator) {
      Object.defineProperty(globalThis, "navigator", originalNavigator);
    } else {
      Reflect.deleteProperty(globalThis, "navigator");
    }
  }
});

test("poster-style generation reuses its durable semantic key after response loss", async () => {
  const semantic = loadSemanticModule();
  const harness = responseLossHarness((path) => {
    assert.equal(path, "/poster-styles/generate");
    return {
      job_id: "poster-job-1",
      workflow_run_id: "poster-job-1",
      status: "running",
      requested_count: 1,
      task_ids: ["generation-1"],
      created_at: "2026-08-03T12:00:00Z",
    };
  });
  const posterStyles = compile("./posterStyles.ts", {
    "./http": { apiFetch: harness.request },
    "./semanticIdempotency": semantic,
  }) as {
    generatePosterStyle(
      body: Record<string, unknown>,
    ): Promise<{ job_id: string; task_ids: string[] }>;
  };
  const body = {
    title: "Retro",
    prompt: "Create a retro poster",
    count: 1,
  };

  await assert.rejects(
    posterStyles.generatePosterStyle(body),
    /response lost/,
  );
  const replay = await posterStyles.generatePosterStyle(body);

  assert.equal(replay.job_id, "poster-job-1");
  assert.deepEqual(replay.task_ids, ["generation-1"]);
  assertReplayPair(harness.calls, 0, { bodyKey: false });

  await assert.rejects(
    posterStyles.generatePosterStyle({
      ...body,
      prompt: "Changed payload",
    }),
    /response lost/,
  );
  assert.notEqual(harness.calls[2]?.key, harness.calls[0]?.key);
});

test("poster-style malformed 2xx acknowledgements retain the semantic key", async () => {
  const semantic = loadSemanticModule();
  const calls: RecordedCall[] = [];
  let attempt = 0;
  const request = async (
    path: string,
    init: RequestInit = {},
  ): Promise<unknown> => {
    const key = new Headers(init.headers).get("Idempotency-Key");
    assert.ok(key);
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    calls.push({ path, key, body });
    attempt += 1;
    return attempt === 1
      ? {}
      : {
          job_id: "poster-job-1",
          workflow_run_id: "poster-workflow-1",
          status: "running",
          requested_count: 1,
          task_ids: ["generation-1"],
          created_at: "2026-08-03T12:00:00Z",
        };
  };
  const posterStyles = compile("./posterStyles.ts", {
    "./http": { apiFetch: request },
    "./semanticIdempotency": semantic,
  }) as {
    generatePosterStyle(
      body: Record<string, unknown>,
    ): Promise<{ job_id: string }>;
  };
  const body = {
    title: "Retro",
    prompt: "Create a retro poster",
    count: 1,
  };

  await assert.rejects(
    posterStyles.generatePosterStyle(body),
    /malformed poster style generation response/,
  );
  assert.equal(
    (await posterStyles.generatePosterStyle(body)).job_id,
    "poster-job-1",
  );
  assertReplayPair(calls, 0, { bodyKey: false });
});

test("paid workflow, storyboard, and video retry callers replay accepted semantic keys", async () => {
  const semantic = loadSemanticModule();
  const harness = responseLossHarness(paidCallerResponse);
  const overrides = {
    "./http": { apiFetch: harness.request },
    "./semanticIdempotency": semantic,
  };
  const workflows = compile("./workflows.ts", overrides) as {
    createApparelWorkflow(body: Record<string, unknown>): Promise<unknown>;
    createModelCandidates(
      workflowId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    createAccessoryPreviews(
      workflowId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    createApparelModelLibraryItem(
      body: Record<string, unknown>,
    ): Promise<unknown>;
    saveModelCandidateToLibrary(
      workflowId: string,
      candidateId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    generateApparelModelLibrary(
      body: Record<string, unknown>,
    ): Promise<unknown>;
    saveApparelModelLibraryJobItem(
      workflowRunId: string,
      imageId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    autoTagApparelModelLibraryItem(itemId: string): Promise<unknown>;
    createShowcaseImages(
      workflowId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    reviseWorkflowImage(
      workflowId: string,
      imageId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
  };
  const posters = compile("./posterWorkflows.ts", overrides) as {
    createPosterDesignWorkflow(
      body: Record<string, unknown>,
    ): Promise<unknown>;
    createPosterMasters(
      workflowId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    createPosterRenders(
      workflowId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    revisePosterRender(
      workflowId: string,
      renderId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    inpaintPosterRender(
      workflowId: string,
      renderId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
  };
  const storyboards = compile("./storyboards.ts", overrides) as {
    generateStoryboardAsset(
      storyboardId: string,
      stepId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    generateStoryboardKeyframe(
      storyboardId: string,
      stepId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    generateAllStoryboardKeyframes(storyboardId: string): Promise<unknown>;
    submitStoryboardShot(
      storyboardId: string,
      stepId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
    submitAllStoryboardShots(storyboardId: string): Promise<unknown>;
  };
  const videos = compile("./videoGenerations.ts", overrides) as {
    retryVideoGeneration(id: string): Promise<unknown>;
  };

  const replay = async (
    invoke: () => Promise<unknown>,
    bodyKey = false,
  ): Promise<void> => {
    const start = harness.calls.length;
    await assert.rejects(invoke(), /response lost/);
    await invoke();
    assertReplayPair(harness.calls, start, { bodyKey });
  };

  await replay(() =>
    workflows.createApparelWorkflow({
      product_image_ids: ["image-1"],
      user_prompt: "showcase",
    }),
  );
  await replay(() =>
    workflows.createModelCandidates("workflow-1", {
      style_prompt: "editorial",
    }),
  );
  await replay(() =>
    workflows.createAccessoryPreviews("workflow-1", {
      candidate_id: "candidate-1",
      accessory_plan: {
        enabled: true,
        items: ["bag"],
        strength: "subtle",
      },
    }),
  );
  await replay(() =>
    workflows.createApparelModelLibraryItem({
      image_id: "image-1",
      title: "Model",
      age_segment: "adult",
    }),
  );
  await replay(() =>
    workflows.saveModelCandidateToLibrary(
      "workflow-1",
      "candidate-1",
      {
        title: "Candidate",
        age_segment: "adult",
      },
    ),
  );
  await replay(() =>
    workflows.generateApparelModelLibrary({
      count: 1,
      auto_tag: true,
    }),
  );
  await replay(() =>
    workflows.saveApparelModelLibraryJobItem(
      "workflow-library",
      "image-1",
      {
        title: "Saved",
        age_segment: "adult",
        gender: "female",
        auto_tag: true,
      },
    ),
  );
  await replay(() =>
    workflows.autoTagApparelModelLibraryItem("library-item-1"),
  );
  await replay(() =>
    workflows.createShowcaseImages("workflow-1", {
      template: "white_ecommerce",
      shot_plan: ["front_full_body"],
      aspect_ratio: "4:5",
      final_quality: "high",
      output_count: 1,
    }),
  );
  await replay(() =>
    workflows.reviseWorkflowImage("workflow-1", "image-1", {
      instruction: "repair",
      scope: "local_repair",
    }),
  );

  await replay(() =>
    posters.createPosterDesignWorkflow({
      copy_text: "Poster",
      style_id: "style-1",
    }),
  );
  await replay(() => posters.createPosterMasters("workflow-1", {}));
  await replay(() =>
    posters.createPosterRenders("workflow-1", { aspects: ["1:1"] }),
  );
  await replay(() =>
    posters.revisePosterRender("workflow-1", "render-1", {
      scope: "style",
      instruction: "brighter",
    }),
  );
  await replay(() =>
    posters.inpaintPosterRender("workflow-1", "render-1", {
      instruction: "remove text",
      mask_image_id: "mask-1",
    }),
  );

  await replay(() =>
    storyboards.generateStoryboardAsset(
      "storyboard-1",
      "asset-1",
      {},
    ),
  );
  await replay(() =>
    storyboards.generateStoryboardKeyframe(
      "storyboard-1",
      "shot-1",
      {},
    ),
  );
  await replay(() =>
    storyboards.generateAllStoryboardKeyframes("storyboard-1"),
  );
  await replay(
    () =>
      storyboards.submitStoryboardShot(
        "storyboard-1",
        "shot-2",
        {},
      ),
    true,
  );
  await replay(() =>
    storyboards.submitAllStoryboardShots("storyboard-1"),
  );
  await replay(() => videos.retryVideoGeneration("video-old"));
});

test("malformed paid-task 2xx responses preserve video, workflow, and storyboard keys", async () => {
  const semantic = loadSemanticModule();
  const calls: RecordedCall[] = [];
  const attempts = new Map<string, number>();
  const request = async (
    path: string,
    init: RequestInit = {},
  ): Promise<unknown> => {
    const key = new Headers(init.headers).get("Idempotency-Key");
    assert.ok(key);
    const body = init.body
      ? (JSON.parse(String(init.body)) as Record<string, unknown>)
      : {};
    calls.push({ path, key, body });
    const attempt = (attempts.get(path) ?? 0) + 1;
    attempts.set(path, attempt);
    if (path === "/videos/generations") {
      return attempt === 1 ? {} : { id: "video-valid" };
    }
    if (path.endsWith("/model-candidates")) {
      return attempt === 1
        ? { id: "workflow-1", steps: [] }
        : workflowTaskRun("model_candidates");
    }
    if (path.endsWith("/shots/shot-2/submit")) {
      return attempt === 1
        ? storyboardTaskRun({
            shotId: "shot-2",
            shotStatus: "generating",
          })
        : storyboardTaskRun({
            shotId: "shot-2",
            shotStatus: "generating",
            videoGenerationId: "video-storyboard",
          });
    }
    throw new Error(`unexpected malformed test path: ${path}`);
  };
  const overrides = {
    "./http": { apiFetch: request },
    "./semanticIdempotency": semantic,
  };
  const videos = compile("./videoGenerations.ts", overrides) as {
    createVideoGeneration(body: Record<string, unknown>): Promise<unknown>;
  };
  const workflows = compile("./workflows.ts", overrides) as {
    createModelCandidates(
      workflowId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
  };
  const storyboards = compile("./storyboards.ts", overrides) as {
    submitStoryboardShot(
      storyboardId: string,
      stepId: string,
      body: Record<string, unknown>,
    ): Promise<unknown>;
  };

  const videoBody = {
    action: "t2v",
    model: "video-model",
    prompt: "malformed",
    duration_s: 5,
    resolution: "1080p",
    aspect_ratio: "16:9",
    generate_audio: false,
    watermark: false,
  };
  await assert.rejects(
    videos.createVideoGeneration(videoBody),
    /malformed video generation response/,
  );
  await videos.createVideoGeneration(videoBody);
  assertReplayPair(calls, 0, { bodyKey: true });

  await assert.rejects(
    workflows.createModelCandidates("workflow-1", {
      style_prompt: "malformed",
    }),
    /malformed workflow task response/,
  );
  await workflows.createModelCandidates("workflow-1", {
    style_prompt: "malformed",
  });
  assertReplayPair(calls, 2, { bodyKey: false });

  await assert.rejects(
    storyboards.submitStoryboardShot(
      "storyboard-1",
      "shot-2",
      {},
    ),
    /malformed storyboard task response/,
  );
  await storyboards.submitStoryboardShot(
    "storyboard-1",
    "shot-2",
    {},
  );
  assertReplayPair(calls, 4, { bodyKey: true });
});
