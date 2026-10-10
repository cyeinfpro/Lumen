import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import "../../../store/chat/moduleResolution.test-helper.mjs";
import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";
import type { VideoOptionsOut } from "@/lib/billingVideoTypes";
import type { CanvasGraph, CanvasNodeExecution } from "@/lib/canvas/types";

const graphDomain = await import("../../../lib/canvas/graph.ts");
const registry = await import("../../../lib/canvas/registry.ts");
const {
  assertCanvasRunIntentMatches,
  canvasActiveNodeIds,
  canvasRunBusyReason,
  canvasUncertainNodeIds,
  canvasRunGraphKey,
  canvasRunScopeMatches,
  createCanvasRunSnapshot,
  loadCanvasRunDiagnostics,
  projectCanvasRunDisabledReasons,
} = await import("./canvasRunReadiness.ts");

const options: VideoOptionsOut = {
  enabled: true,
  models: [{
    model: "video-test", actions: ["t2v"], resolutions: ["720p"],
    durations_s: [5],
  }],
  durations_s: [5], resolutions: ["720p"], aspect_ratios: ["16:9"],
  generate_audio: true, pricing: [], hold_estimates: {},
};

function fixture(videoCount = 2): CanvasGraph {
  const graph = graphDomain.createDefaultCanvasGraph();
  graph.nodes[0]!.config.text = "镜头缓慢推进";
  for (let index = 0; index < videoCount; index += 1) {
    const video = registry.createCanvasNode("video_text_generate", { x: 720, y: index * 250 }, {
      id: `video-${index}`,
    });
    graph.nodes.push(video);
    graph.edges.push(graphDomain.createCanvasEdge(graph, {
      sourceNodeId: "prompt-1", sourceHandle: "text",
      targetNodeId: video.id, targetHandle: "prompt",
    })!);
  }
  return graph;
}

function project(
  snapshot: ReturnType<typeof createCanvasRunSnapshot>,
  diagnostics?: Awaited<ReturnType<typeof loadCanvasRunDiagnostics>>,
  failed = false,
) {
  return projectCanvasRunDisabledReasons({
    snapshot, diagnostics, failed, runningNodeIds: new Set(), saveState: "saved",
  });
}

test("all video nodes share one read-only options lookup and the existing diagnostics", async () => {
  const snapshot = createCanvasRunSnapshot("workflow-a", 4, fixture(8));
  let requests = 0;
  const diagnostics = await loadCanvasRunDiagnostics(snapshot, async () => {
    requests += 1;
    return { ...options, enabled: false, unavailable_reason: "account_mode_forbidden" };
  }, new AbortController().signal);
  assert.equal(requests, 1);
  const reasons = project(snapshot, diagnostics);
  for (const id of snapshot.videoNodeIds) {
    assert.equal(reasons.get(id), "BYOK 模式暂不支持视频生成");
  }
  assert.equal(reasons.get("image-generate-1"), null);
});

test("image-only graphs do not request video capabilities", async () => {
  const snapshot = createCanvasRunSnapshot("image-only", 1, fixture(0));
  const diagnostics = await loadCanvasRunDiagnostics(snapshot, async () => {
    assert.fail("Unexpected capability lookup");
  }, new AbortController().signal);
  assert.equal(project(snapshot, diagnostics).get("image-generate-1"), null);
});

test("capability diagnostics fence workflow, revision, and unsaved graph changes", async () => {
  const graph = fixture();
  const snapshot = createCanvasRunSnapshot("workflow-a", 4, graph);
  const diagnostics = await loadCanvasRunDiagnostics(snapshot, async () => options, new AbortController().signal);
  assert.equal(canvasRunScopeMatches(snapshot, diagnostics), true);
  const differentScopes = [
    createCanvasRunSnapshot("workflow-b", 4, graph),
    createCanvasRunSnapshot("workflow-a", 5, graph),
    createCanvasRunSnapshot("workflow-a", 4, {
      ...graph,
      nodes: graph.nodes.map((node) => node.id === "video-0"
        ? { ...node, config: { ...node.config, resolution: "1080p" } } : node),
    }),
  ];
  for (const current of differentScopes) {
    assert.equal(canvasRunScopeMatches(current, diagnostics), false);
    assert.equal(project(current, diagnostics).get("video-0"), "视频能力检查中");
  }
});

test("late and cancelled responses never publish into a newer revision", async () => {
  const first = createCanvasRunSnapshot("workflow-a", 4, fixture());
  const second = createCanvasRunSnapshot("workflow-a", 5, fixture());
  let resolve!: (value: VideoOptionsOut) => void;
  const delayed = new Promise<VideoOptionsOut>((done) => { resolve = done; });
  const controller = new AbortController();
  const pending = loadCanvasRunDiagnostics(first, () => delayed, controller.signal);
  controller.abort();
  resolve(options);
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(project(second).get("video-0"), "视频能力检查中");
});

test("transient capability failures preserve structural errors and allow a fresh run or retry", async () => {
  const graph = fixture();
  graph.nodes.push(registry.createCanvasNode("video_text_generate", { x: 1000, y: 0 }, {
    id: "missing-prompt",
  }));
  const snapshot = createCanvasRunSnapshot("workflow-a", 4, graph);
  await assert.rejects(
    loadCanvasRunDiagnostics(snapshot, async () => { throw new Error("offline"); }, new AbortController().signal),
    /offline/,
  );
  const failed = project(snapshot, undefined, true);
  assert.equal(failed.get("video-0"), null);
  assert.ok(failed.get("missing-prompt"));
  const recovered = await loadCanvasRunDiagnostics(snapshot, async () => ({
    ...options, enabled: false, unavailable_reason: "account_mode_forbidden",
  }), new AbortController().signal);
  assert.equal(project(snapshot, recovered).get("video-0"), "BYOK 模式暂不支持视频生成");
});

test("active, submitting, and conflicted nodes share the same blocking reason", async () => {
  const snapshot = createCanvasRunSnapshot("workflow-a", 4, fixture());
  const diagnostics = await loadCanvasRunDiagnostics(snapshot, async () => options, new AbortController().signal);
  const busy = projectCanvasRunDisabledReasons({
    snapshot, diagnostics, failed: false,
    runningNodeIds: new Set(["video-0", "image-generate-1"]), saveState: "saved",
  });
  assert.equal(busy.get("video-0"), "节点运行中，等待当前任务完成");
  assert.equal(busy.get("image-generate-1"), busy.get("video-0"));
  const conflict = projectCanvasRunDisabledReasons({
    snapshot, diagnostics, failed: false, runningNodeIds: new Set(), saveState: "conflict",
  });
  assert.equal(conflict.get("video-0"), "画布存在保存冲突，解决后再运行");
});

test("layout-only movement keeps the semantic cache key; configuration and bindings invalidate it", () => {
  const graph = fixture();
  const key = canvasRunGraphKey(graph);
  assert.equal(canvasRunGraphKey({
    ...graph, nodes: graph.nodes.map((node) => ({ ...node, position: { x: 800, y: 500 } })),
  }), key);
  assert.notEqual(canvasRunGraphKey({
    ...graph, edges: graph.edges.map((edge) => ({ ...edge, binding_mode: "pinned" })),
  }), key);
  graph.nodes[0]!.config.text = "新的提示词";
  assert.notEqual(canvasRunGraphKey(graph), key);
});

test("command menu consumes the same reason as node and inspector, including busy and retryable states", () => {
  const { buildCommandItems } = loadTsModule(new URL("./canvasWorkspaceToolDomain.ts", import.meta.url), {
    "lucide-react": {},
    "@/lib/canvas/graph": graphDomain,
    "@/lib/canvas/registry": registry,
    "@/components/ui/primitives": { toast: {} },
  });
  for (const reason of [null, "当前视频模型不可用，重新选择", "节点运行中，等待当前任务完成"]) {
    const items = buildCommandItems({
      graph: fixture(), actionRequest: null, selectedNodeId: "video-0",
      selectedCount: 1, selectedEdgeId: null,
      runDisabledReasons: new Map([["video-0", reason]]),
    });
    const item = items.find((candidate: { id: string }) => candidate.id === "run:selected");
    assert.equal(item.disabled, Boolean(reason));
    assert.equal(item.description, reason ?? undefined);
  }
});

test("all three surfaces share readiness, while the final run keeps validation, flush, and fencing", () => {
  const source = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
  for (const file of ["./CanvasInspector.tsx", "./useCanvasViewportProjection.ts", "./useCanvasWorkspaceTools.ts"]) {
    assert.match(source(file), /useCanvasRunReadiness/);
  }
  const provider = source("./CanvasRunReadinessProvider.tsx");
  assert.match(provider, /PREFLIGHT_DEBOUNCE_MS = 250/);
  assert.match(provider, /queryKey: \["canvas-run-readiness", document.id, revision, snapshot.graphKey\]/);
  assert.doesNotMatch(provider, /mutateAsync|executeCanvasNode|\/execute/);
  const workspace = source("./CanvasWorkspace.tsx");
  assert.match(workspace, /validateCanvasNodeExecution\(checkedGraph, nodeId\)/);
  assert.match(workspace, /canvasVideoCapabilityError/);
  assert.match(workspace, /await autosaveRef.current\?\.flush\(\)/);
  assert.match(workspace, /assertCanvasRunIntentMatches\(state.graph, checkedGraphKey\)/);
  assert.match(workspace, /executeNode.mutateAsync\(\{ nodeId, revision: state.revision \}\)/);
  assert.match(workspace, /canvasRunBusyReason\(nodeId, activeNodeIds, submittingNodeIdsRef.current, uncertainNodeIds\)/);
});

test("expired owner with unknown-submit error blocks every readiness entry without a running spinner", () => {
  const execution: CanvasNodeExecution = { id: "uncertain", node_id: "video-0", node_type: "video_text_generate", status: "failed", outputs: [],
    tasks: [{ id: "task", kind: "video_generation", status: "expired", progress_stage: "finished", error_code: "submit_unknown", finished_at: "2026-10-10" }] };
  const document = { recent_executions: [execution], active_runs: [] };
  const uncertainNodeIds = canvasUncertainNodeIds(document);
  assert.equal(uncertainNodeIds.has("video-0"), true);
  assert.equal(canvasRunBusyReason("video-0", new Set(), new Set(), uncertainNodeIds), "上次提交状态待确认，请先查询原任务");
  assert.equal(canvasActiveNodeIds(document).has("video-0"), false, "uncertainty blocks execution without pretending to be active");
  const reasons = projectCanvasRunDisabledReasons({ snapshot: createCanvasRunSnapshot("canvas", 1, fixture()),
    failed: false, runningNodeIds: canvasActiveNodeIds(document), uncertainNodeIds, saveState: "saved" });
  assert.equal(reasons.get("video-0"), "上次提交状态待确认，请先查询原任务");
  assert.equal(canvasActiveNodeIds({ recent_executions: [{ ...execution, status: "running" }],
    active_runs: [{ id: "r", status: "running", target_node_ids: ["video-0"] }] }).has("video-0"), false);
  execution.tasks![0]!.error_code = null;
  assert.equal(canvasUncertainNodeIds(document).has("video-0"), false);
  assert.equal(canvasActiveNodeIds(document).has("video-0"), false);
});

test("old active history cannot override a newer terminal execution; explicit active runs still block", () => {
  const execution = (id: string, nodeId: string, status: CanvasNodeExecution["status"]): CanvasNodeExecution => ({
    id, node_id: nodeId, node_type: "video_text_generate", status, outputs: [],
  });
  const recent_executions = [
    execution("new", "video-0", "succeeded"),
    execution("running", "video-1", "running"),
    execution("old-pinned", "video-0", "queued"),
  ];
  assert.deepEqual([...canvasActiveNodeIds({ recent_executions, active_runs: [] })], ["video-1"]);
  assert.deepEqual([...canvasActiveNodeIds({
    recent_executions,
    active_runs: [{ id: "run-0", status: "planning", target_node_ids: ["video-0"] }],
  })], ["video-1", "video-0"]);
});

test("the final intent fence permits layout-only changes and rejects changed execution inputs", () => {
  const graph = fixture();
  const checkedKey = canvasRunGraphKey(graph);
  graph.nodes[0]!.position.x += 10;
  assert.doesNotThrow(() => assertCanvasRunIntentMatches(graph, checkedKey));
  graph.nodes[0]!.config.text = "新的输入";
  assert.throws(() => assertCanvasRunIntentMatches(graph, checkedKey), /节点配置已变化/);
});

test("existing identity reset clears shared video options and readiness, including late responses", async () => {
  const { QueryClient } = await import("@tanstack/react-query");
  const { clearPreviousUserQueryCache } = loadTsModule(
    new URL("../../../lib/queries/userScope.ts", import.meta.url),
    { "@/store/useChatStore": {} },
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  client.setQueryData(["video-options"], options);
  client.setQueryData(["canvas-run-readiness", "workflow-a", 4, "graph-key"], { user: "previous" });
  clearPreviousUserQueryCache(client, "previous-user");
  assert.equal(client.getQueryData(["video-options"]), undefined);
  assert.equal(client.getQueryData(["canvas-run-readiness", "workflow-a", 4, "graph-key"]), undefined);
  let release!: (value: VideoOptionsOut) => void;
  const delayed = new Promise<VideoOptionsOut>((resolve) => { release = resolve; });
  const pending = client.fetchQuery({ queryKey: ["video-options"], queryFn: () => delayed });
  const cancelled = pending.catch(() => undefined);
  clearPreviousUserQueryCache(client, "previous-user");
  release(options);
  await cancelled;
  assert.equal(client.getQueryData(["video-options"]), undefined);
  client.clear();
});
