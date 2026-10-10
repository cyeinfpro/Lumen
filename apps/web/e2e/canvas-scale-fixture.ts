import { readFile } from "node:fs/promises";
import { expect, type Page } from "@playwright/test";
import sharp from "sharp";
import { installAgentFixture } from "./agent-fixture";
import type { CanvasGraph, CanvasNodeExecution, CanvasNodeSelection } from "../src/lib/canvas/types";
const NOW = "2026-10-10T14:00:00Z";
type Stream = EventTarget & { readyState: number };
type ScaleWindow = Window & { __scaleStreams: Stream[] };
export const canvasNode = (page: Page, id: string) => page.locator('.react-flow__node[data-id="' + id + '"]');

export async function installScaleFixture(page: Page, count: number) {
  await installAgentFixture(page, { canvasEnabled: true });
  await page.addInitScript(() => {
    const streams: Stream[] = [];
    (window as unknown as ScaleWindow).__scaleStreams = streams;
    class ScaleEventSource extends EventTarget {
      static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
      readyState = 0; url: string;
      onopen: ((event: Event) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      constructor(url: string) {
        super(); this.url = String(url); streams.push(this);
        queueMicrotask(() => { this.readyState = 1; this.onopen?.(new Event("open")); });
      }
      close() { this.readyState = 2; }
    }
    Object.defineProperty(window, "EventSource", { value: ScaleEventSource, configurable: true });
  });
  const columns = Math.ceil(Math.sqrt(count));
  // Choose a complete prompt/image/video triplet nearest the graph's geometric centre.
  // A fixed corner is correctly virtualized out after fitView reaches its minimum zoom.
  const centre = { x: 40 + ((columns - 1) * 340 + 260) / 2, y: 40 + ((Math.ceil(count / columns) - 1) * 300 + 220) / 2 };
  let anchor = 0, nearest = Infinity;
  for (let i = 0; i + 2 < count; i += 3) {
    if (Math.floor(i / columns) !== Math.floor((i + 2) / columns)) continue;
    const x = 40 + (i % columns + 1) * 340 + 130, y = 40 + Math.floor(i / columns) * 300 + 110;
    const distance = (x - centre.x) ** 2 + (y - centre.y) ** 2;
    if (distance < nearest) { nearest = distance; anchor = i; }
  }
  const graph: CanvasGraph = {
    schema_version: 1,
    nodes: Array.from({ length: count }, (_, i) => ({
      id: "n-" + i, title: "Scale " + i, schema_version: 1,
      type: i % 3 === 0 ? "prompt" : i % 3 === 1 ? "image_generate" : "video_text_generate",
      position: { x: 40 + (i % columns) * 340, y: 40 + Math.floor(i / columns) * 300 },
      size: { width: 260, height: 220 },
      config: i % 3 === 0 ? { text: "Deterministic scale prompt " + i, locked: false } : {}, ui: {},
    })),
    edges: [], frames: [], settings: { snap_to_grid: false, grid_size: 16 },
  };
  for (let i = 0; i < count; i++) if (i % 3 !== 0) graph.edges.push({
    id: "e-" + i, source_node_id: "n-" + (i - i % 3), source_handle: "text",
    target_node_id: "n-" + i, target_handle: "prompt", data_type: "text", binding_mode: "follow_active",
  });
  const executions = graph.nodes.flatMap((n, i) => i % 3 ? [{
    id: "execution-" + i, run_id: "scale-run", node_id: n.id, node_type: n.type,
    status: "succeeded", created_at: NOW, updated_at: NOW, tasks: [],
    outputs: i % 3 === 1 ? [0, 1].map((v) => ({
      type: "image", image_id: "image-" + i + "-" + v, url: "/api/scale-media/" + i + "-" + v + ".png",
      preview_url: "/api/scale-media/" + i + "-" + v + ".png", width: 640, height: 360,
    })) : [{
      type: "video", video_id: "video-" + i, url: "/api/scale-media/" + i + ".mp4",
      poster_url: "/api/scale-media/" + i + "-poster.png", width: 640, height: 360, duration_ms: 1000,
    }],
  } as CanvasNodeExecution] : []);
  const selections: CanvasNodeSelection[] = executions.map((e) => ({
    node_id: e.node_id, execution_id: e.id, output_index: 0, revision: 1,
  }));
  let revision = 4, seq = 0, snapshotGap = false, statusRound = 0;
  const counts = { snapshots: 0, mutations: 0, batches: 0, details: 0, selections: 0, images: 0, video: 0, prohibited: 0 };
  const requests: Array<{ kind: string; body: unknown }> = [];
  const document = () => ({
    id: "canvas-scale", title: "Scale " + count, description: "", revision, graph,
    recent_executions: executions, selections, active_runs: [], created_at: NOW, updated_at: NOW,
  });
  const pixels = Buffer.alloc(640 * 360 * 3);
  for (let i = 0; i < pixels.length; i++) pixels[i] = (i * 31 + Math.floor(i / 1920) * 17) % 256;
  const png = await sharp(pixels, { raw: { width: 640, height: 360, channels: 3 } }).png().toBuffer();
  const mp4 = process.env.LUMEN_SCALE_VIDEO ? await readFile(process.env.LUMEN_SCALE_VIDEO) : null;
  await page.route("**/api/scale-media/**", (route) => {
    if (route.request().url().endsWith(".mp4")) {
      counts.video++;
      return mp4 ? route.fulfill({ contentType: "video/mp4", body: mp4 }) : route.abort("connectionfailed");
    }
    counts.images++;
    return route.fulfill({ contentType: "image/png", body: png });
  });
  await page.route("**/api/videos/options", (route) => route.fulfill({ json: {
    enabled: true, models: [{ model: "fixture-video", actions: ["t2v"], resolutions: ["720p"], durations_s: [5] }],
    durations_s: [5], resolutions: ["720p"], aspect_ratios: ["16:9"], generate_audio: true, pricing: [], hold_estimates: {},
  } }));
  await page.route("**/api/canvases/canvas-scale**", (route) => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (path.endsWith("/mutations")) {
      counts.mutations++;
      const body = request.postDataJSON(); requests.push({ kind: "mutation", body });
      for (const op of body.operations ?? []) {
        const n = graph.nodes.find((item) => item.id === op.node_id);
        if (n && op.op === "update_node_config") n.config = op.config;
        if (op.op === "move_nodes") for (const p of op.items) {
          const moving = graph.nodes.find((item) => item.id === p.node_id);
          if (moving) moving.position = { x: p.x, y: p.y };
        }
      }
      return route.fulfill({ json: { revision: ++revision } });
    }
    if (path.endsWith("/select")) {
      counts.selections++;
      const body = request.postDataJSON(); requests.push({ kind: "selection", body });
      const id = decodeURIComponent(path.split("/").at(-2)!);
      const e = executions.find((item) => item.id === id)!;
      const selection = selections.find((item) => item.node_id === e.node_id)!;
      if (body.selection_revision !== selection.revision) return route.fulfill({ status: 409, json: { detail: "selection conflict" } });
      Object.assign(selection, { output_index: body.output_index, revision: (selection.revision ?? 0) + 1 });
      return route.fulfill({ json: selection });
    }
    if (request.method() !== "GET") {
      counts.prohibited++; return route.fulfill({ status: 400, json: { detail: "No generation or external writes in scale fixture" } });
    }
    if (path.endsWith("/event-batch")) {
      counts.batches++;
      const after = Number(url.searchParams.get("after_seq"));
      return route.fulfill({ json: { items: Array.from({ length: seq - after }, (_, i) => ({ run_id: "scale-run", seq: after + i + 1, payload: {} })),
        after_seq: after, next_after_seq: seq, last_event_seq: seq,
        has_more: false, snapshot_required: snapshotGap } });
    }
    if (path.endsWith("/runs/scale-run")) {
      counts.details++;
      return route.fulfill({ json: { id: "scale-run", status: "succeeded", last_event_seq: seq, executions } });
    }
    if (path.endsWith("/executions")) return route.fulfill({ json: { items: executions.filter((e) => e.node_id === url.searchParams.get("node_id")), next_cursor: null } });
    counts.snapshots++;
    return route.fulfill({ json: document() });
  });
  return {
    graph, counts, requests, selections, anchor,
    dimensions: { nodes: count, edges: graph.edges.length, imageNodes: executions.filter((e) => e.node_type === "image_generate").length,
      videoNodes: executions.filter((e) => e.node_type === "video_text_generate").length, pngBytes: png.length, mp4Bytes: mp4?.length ?? 0 },
    next(gap = false) {
      seq += 32; snapshotGap = gap; statusRound++;
      const e = executions.find((item) => item.id === "execution-" + (anchor + 1))!;
      e.status = statusRound % 2 ? "running" : "succeeded";
      return { seq, status: e.status };
    },
    async emit(sequence: number, burst = 32) {
      await expect.poll(() => page.evaluate(() => (window as unknown as ScaleWindow).__scaleStreams.filter((s) => s.readyState === 1).length)).toBeGreaterThan(0);
      await page.evaluate(({ sequence, burst, executionId }) => {
        for (let i = 0; i < burst; i++) {
          const payload = { schema_version: 1, canvas_id: "canvas-scale", run_id: "scale-run", seq: sequence,
            execution_id: executionId, event_type: "canvas.execution.status_changed", event_id: "canvas-run:scale-run:" + sequence };
          for (const source of (window as unknown as ScaleWindow).__scaleStreams) if (source.readyState === 1)
            source.dispatchEvent(new MessageEvent("canvas.run.updated", { data: JSON.stringify(payload), lastEventId: sequence + "-0" }));
        }
      }, { sequence, burst, executionId: "execution-" + (anchor + 1) });
    },
  };
}
