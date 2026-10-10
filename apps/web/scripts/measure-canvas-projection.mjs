#!/usr/bin/env node
// Synthetic CPU projection measurements only, not browser rendering or provider throughput.
// Run in a coordinated, otherwise idle local test window; no requests or task submissions.
import assert from "node:assert/strict";
import { cpus, loadavg, platform, arch } from "node:os";
import { performance } from "node:perf_hooks";
import { activeOutputsByNode } from "../src/lib/canvas/runtime.ts";

function graph(size) {
  return {
    graph: { schema_version: 1, nodes: Array.from({ length: size }, (_, i) => ({
      id: "n" + i, type: "image_generate", schema_version: 1, title: "Synthetic " + i,
      position: { x: (i % 25) * 320, y: Math.floor(i / 25) * 260 }, config: {}, ui: {},
    })), edges: [], frames: [], settings: { snap_to_grid: false, grid_size: 16 } },
    selections: Array.from({ length: size }, (_, i) => ({ node_id: "n" + i,
      execution_id: "e" + i, output_index: 0 })),
    recent_executions: Array.from({ length: size }, (_, i) => ({ id: "e" + i,
      node_id: "n" + i, node_type: "image_generate", status: "succeeded",
      outputs: [{ type: "image", image_id: "i" + i }] })),
  };
}
function referenceProjection(document) {
  const executions = new Map(document.recent_executions.map((execution) => [execution.id, execution]));
  const outputs = new Map();
  for (const node of document.graph.nodes) {
    const selection = document.selections.find((candidate) =>
      candidate.node_id === node.id && candidate.execution_id !== null);
    const output = selection && executions.get(selection.execution_id)?.outputs[selection.output_index];
    if (output) outputs.set(node.id, output);
  }
  return outputs;
}
function sample(fn, document) {
  const start = performance.now();
  for (let i = 0; i < 25; i += 1) fn(document);
  return (performance.now() - start) / 25;
}
const results = [];
for (const size of [100, 500, 1000]) {
  const document = graph(size);
  assert.deepEqual(activeOutputsByNode(document), referenceProjection(document));
  for (let i = 0; i < 3; i += 1) {
    sample(referenceProjection, document); sample(activeOutputsByNode, document);
  }
  const indexed = [], reference = [];
  // Alternate order to reduce warm-cache and thermal-order bias.
  for (let i = 0; i < 8; i += 1) {
    if (i % 2 === 0) {
      indexed.push(sample(activeOutputsByNode, document));
      reference.push(sample(referenceProjection, document));
    } else {
      reference.push(sample(referenceProjection, document));
      indexed.push(sample(activeOutputsByNode, document));
    }
  }
  results.push({ nodes: size, selections: size, execution_outputs: size,
    correctness: "equal", indexed_ms_per_projection: indexed,
    reference_linear_scan_ms_per_projection: reference });
}
process.stdout.write(JSON.stringify({
  schema_version: 1, measured_at: new Date().toISOString(),
  environment: { node: process.version, platform: platform(), arch: arch(),
    cpu_count: cpus().length, load_average: loadavg() },
  scope: "synthetic synchronous output projection; excludes DOM, layout, media, network and rendering",
  results,
}, null, 2) + "\n");
