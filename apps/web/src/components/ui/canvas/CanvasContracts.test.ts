import { doesNotMatch, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const workspaceSource = source("./CanvasWorkspace.tsx");
const viewportSource = source("./CanvasViewport.tsx");
const topBarSource = source("./CanvasTopBar.tsx");
const mobileToolbarSource = source("./mobile/CanvasMobileToolbar.tsx");
const nodesSource = source("./nodes/CanvasNodes.tsx");
const inspectorSource = source("./CanvasInspector.tsx");
const querySource = source("../../../lib/queries/canvases.ts");
const manifestSource = source("../../../app/manifest.ts");

test("canvas fullscreen is distinct from fit view and keeps portal overlays available", () => {
  match(workspaceSource, /document\.documentElement/);
  match(workspaceSource, /requestFullscreen/);
  match(workspaceSource, /fullscreenchange/);
  match(topBarSource, /aria-label=\{fullscreen \? "退出全屏" : "全屏画布"\}/);
  match(topBarSource, /<Scan className="h-4 w-4" \/>/);
});

test("canvas compact layout uses a stable workbench breakpoint and safe areas", () => {
  match(workspaceSource, /max-width: 1199px/);
  match(workspaceSource, /min-\[1200px\]:grid-cols/);
  match(topBarSource, /safe-area-inset-top/);
  match(topBarSource, /system-banner-height/);
  match(mobileToolbarSource, /safe-area-inset-left/);
  match(mobileToolbarSource, /safe-area-inset-right/);
  doesNotMatch(viewportSource, /justify-center gap-2 md:hidden/);
  doesNotMatch(manifestSource, /orientation:\s*"portrait"/);
});

test("canvas movement, deletion, and frame layering use domain state", () => {
  match(viewportSource, /splitCanvasNodePositionChanges/);
  match(viewportSource, /measuredDimensions/);
  match(viewportSource, /change\.type === "dimensions"/);
  match(viewportSource, /moveNodes\(settled\)/);
  match(viewportSource, /onBeforeDelete/);
  match(viewportSource, /removeElements/);
  match(viewportSource, /onNodeClick/);
  match(viewportSource, /elevateNodesOnSelect=\{false\}/);
  match(viewportSource, /initialHeight/);
  match(viewportSource, /getViewportCenter/);
  match(viewportSource, /connectOnClick=\{false\}/);
  match(viewportSource, /startClickConnection/);
  match(nodesSource, /onStartConnection/);
  match(nodesSource, /isConnectableStart=\{direction === "output"\}/);
  match(nodesSource, /isConnectableEnd=\{direction === "input"\}/);
  match(nodesSource, /overflow-visible/);
});

test("canvas text and titles edit directly inside deliberate drag handles", () => {
  match(viewportSource, /dragHandle: "\.canvas-node-drag-handle"/);
  match(nodesSource, /canvas-node-drag-handle/);
  match(nodesSource, /<textarea/);
  match(nodesSource, /onUpdateConfig/);
  match(nodesSource, /onUpdateTitle/);
  match(nodesSource, /onEditFocus/);
  match(nodesSource, /nodrag nopan nowheel nokey/);
  match(nodesSource, /nativeEvent\.isComposing/);
  match(nodesSource, /onCompositionEnd/);
  match(nodesSource, /window\.setTimeout\(flush, 180\)/);
  match(nodesSource, /readOnly=\{editingDisabled\}/);
  match(nodesSource, /cancelBlurRef/);
  match(nodesSource, /editingEnabled === false/);
  match(nodesSource, /MAX_PROMPT_CHARS/);
  match(viewportSource, /current\.getZoom\(\) >= 0\.75/);
  match(viewportSource, /minZoom: 0\.9/);
  match(viewportSource, /ariaLabel: `\$\{CANVAS_NODE_SPECS/);
  match(viewportSource, /aria-label="无限画布编辑区"/);
  doesNotMatch(inspectorSource, /function TextNodeConfig/);
  doesNotMatch(inspectorSource, /<Textarea/);
});

test("compact canvas keeps inspector explicit and exposes redo", () => {
  match(workspaceSource, /open=\{isCompact && inspectorOpen\}/);
  doesNotMatch(workspaceSource, /inspectorOpen \|\| Boolean\(selectedNodeId\)/);
  match(mobileToolbarSource, /label="重做"/);
});

test("canvas output selections notify other tabs", () => {
  match(querySource, /canvas\.selection\.changed/);
  match(workspaceSource, /payload\.type === "canvas\.selection\.changed"/);
});

test("canvas autosave retries exact batches and only publishes accepted acknowledgements", () => {
  match(workspaceSource, /new RetryableAutosaveBatchReader/);
  match(workspaceSource, /takeAutosaveOperations/);
  match(workspaceSource, /mutation_id: batch\.payload\.mutationId/);
  match(workspaceSource, /putCanvasSaveBatch/);
  match(workspaceSource, /getCanvasSaveBatch/);
  match(workspaceSource, /canvasSaveBatchMatchesPending/);
  match(workspaceSource, /markSaving\(batch\.count\)/);
  match(workspaceSource, /if \(!acknowledged\) return/);
  match(workspaceSource, /current\.revision <= savedRevision/);
  match(workspaceSource, /decideCanvasRemoteSync/);
  match(workspaceSource, /onOnlineRestore\(flush\)/);
  match(workspaceSource, /blurActiveCanvasEditor/);
  match(workspaceSource, /visibilitychange/);
  match(workspaceSource, /state\.pendingOperations\.length === 0/);
  match(workspaceSource, /CANVAS_CLIENT_LEASE_TTL_MS/);
  match(workspaceSource, /listCanvasDrafts/);
});
