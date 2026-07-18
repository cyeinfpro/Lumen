import { doesNotMatch, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const workspaceSource = source("./CanvasWorkspace.tsx");
const workspaceInteractionsSource = source("./CanvasWorkspaceInteractions.ts");
const workspacePersistenceSource = source("./CanvasWorkspacePersistence.ts");
const viewportSource = source("./CanvasViewport.tsx");
const topBarSource = source("./CanvasTopBar.tsx");
const mobileToolbarSource = source("./mobile/CanvasMobileToolbar.tsx");
const nodesSource = source("./nodes/CanvasNodes.tsx");
const imageAssetDropSource = source("./nodes/CanvasImageAssetDropZone.tsx");
const inspectorSource = source("./CanvasInspector.tsx");
const nodeConfigEditorSource = source("./CanvasNodeConfigEditor.tsx");
const outputDownloadSource = source("./CanvasOutputDownloadButton.tsx");
const videoPreviewSource = source("./CanvasVideoPreviewDialog.tsx");
const workspaceToolsSource = source("./useCanvasWorkspaceTools.ts");
const commandMenuSource = source("./CanvasCommandMenu.tsx");
const viewportControlsSource = source("./CanvasViewportControls.tsx");
const paletteSource = source("./CanvasNodePalette.tsx");
const clipboardSource = source("../../../lib/canvas/clipboard.ts");
const querySource = source("../../../lib/queries/canvases.ts");
const apiSource = source("../../../lib/api/canvases.ts");
const constantsSource = source("../../../lib/canvas/constants.ts");
const manifestSource = source("../../../app/manifest.ts");

test("canvas fullscreen is distinct from fit view and keeps portal overlays available", () => {
  match(workspaceInteractionsSource, /document\.documentElement/);
  match(workspaceInteractionsSource, /requestFullscreen/);
  match(workspaceInteractionsSource, /fullscreenchange/);
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

test("canvas interactions cleanly settle drafts, cancellation, deletion, and resize state", () => {
  match(viewportSource, /connectionDraftRef/);
  match(viewportSource, /updateConnectionDraft/);
  match(viewportSource, /new Event\("touchend"/);
  match(viewportSource, /onPointerCancelCapture/);
  match(viewportSource, /onTouchCancelCapture/);
  match(
    viewportSource,
    /const cancelled = cancelledResizeRef\.current;\s*cancelledResizeRef\.current = false;\s*if \(!cancelled\) \{\s*resizeNode/,
  );
  match(
    viewportSource,
    /if \(cancelledConnectionRef\.current\) return;/,
  );
  match(viewportSource, /const cancelledResizeRef = useRef\(false\)/);
  match(viewportSource, /cancelledResizeRef\.current = false/);
  doesNotMatch(
    viewportSource,
    /if \(!cancelledConnectionRef\.current\) \{\s*resizeNode/,
  );
  match(viewportSource, /cancelDomainInteraction/);
  match(
    viewportSource,
    /interactionActiveRef\.current \|\|\s*resizingNodeIdsRef\.current\.size > 0 \|\|\s*connectionDraftRef\.current/,
  );
  match(viewportSource, /change\.resizing === false/);
  match(
    viewportSource,
    /resizeNode\(nodeId, geometry\.size, geometry\.position\)/,
  );
  match(viewportSource, /clearTransientNodeState\(\[nodeId\]\)/);
  match(
    viewportSource,
    /onStartConnection: clickConnectionEnabled\s*\?\s*startClickConnection\s*:\s*undefined/,
  );
  match(nodesSource, /onResize=\{\(\) => data\.onResizeStart\?\.\(definition\.id\)\}/);
  match(nodesSource, /x: Math\.round\(params\.x\)/);
  match(nodesSource, /y: Math\.round\(params\.y\)/);
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
  match(viewportSource, /findMatchingCanvasNodeCatalogItem\(node\)/);
  match(viewportSource, /ariaLabel: `\$\{preset\?\.label \?\? CANVAS_NODE_SPECS/);
  match(viewportSource, /aria-label="无限画布编辑区"/);
  doesNotMatch(inspectorSource, /function TextNodeConfig/);
  doesNotMatch(inspectorSource, /<Textarea/);
});

test("canvas keeps inspector explicit across mobile, tablet, and desktop", () => {
  match(
    workspaceSource,
    /const showTabletInspector = !isMobile && isCompact && open/,
  );
  match(workspaceSource, /<BottomSheet\s+open=\{open\}/);
  match(workspaceSource, /\{hasSelection \? \(/);
  doesNotMatch(workspaceSource, /inspectorOpen \|\| Boolean\(selectedNodeId\)/);
  match(mobileToolbarSource, /label="重做"/);
});

test("canvas run controls recognize every executable registry node", () => {
  match(topBarSource, /isCanvasExecutableNodeType\(selectedNode\.type\)/);
  match(topBarSource, /validateCanvasNodeExecution\(graph, selectedNode\.id\)/);
  doesNotMatch(
    topBarSource,
    /selectedNode\?\.type === "image_generate"/,
  );
});

test("multi-input connections expose explicit order controls", () => {
  match(inspectorSource, /aria-label="输入上移"/);
  match(inspectorSource, /aria-label="输入下移"/);
  match(inspectorSource, /onUpdateDetails\(edge\.id, \{ order \}\)/);
});

test("canvas output selections notify other tabs", () => {
  match(querySource, /canvas\.selection\.changed/);
  match(
    workspacePersistenceSource,
    /payload\.type === "canvas\.selection\.changed"/,
  );
});

test("canvas autosave retries exact batches and only publishes accepted acknowledgements", () => {
  match(workspacePersistenceSource, /new RetryableAutosaveBatchReader/);
  match(workspacePersistenceSource, /takeAtomicAutosaveOperations/);
  match(workspacePersistenceSource, /state\.pendingOperationGroupSizes/);
  match(workspacePersistenceSource, /mutation_id: batch\.payload\.mutationId/);
  match(workspacePersistenceSource, /putCanvasSaveBatch/);
  match(workspacePersistenceSource, /getCanvasSaveBatch/);
  match(workspacePersistenceSource, /canvasSaveBatchMatchesPending/);
  match(workspacePersistenceSource, /markSaving\(batch\.count\)/);
  match(workspacePersistenceSource, /if \(!acknowledged\) return/);
  match(workspaceSource, /current\.revision <= savedRevision/);
  match(workspacePersistenceSource, /decideCanvasRemoteSync/);
  match(workspacePersistenceSource, /onOnlineRestore\(\(\) => \{/);
  match(workspacePersistenceSource, /blurActiveCanvasEditor/);
  match(workspacePersistenceSource, /visibilitychange/);
  match(workspacePersistenceSource, /state\.pendingOperations\.length === 0/);
  match(workspacePersistenceSource, /CANVAS_CLIENT_LEASE_TTL_MS/);
  match(
    workspacePersistenceSource,
    /CANVAS_SUSPENDED_CLIENT_LEASE_TTL_MS/,
  );
  match(workspacePersistenceSource, /event\.persisted/);
  match(workspacePersistenceSource, /pageshow/);
  match(workspacePersistenceSource, /listCanvasDrafts/);
});

test("canvas workbench exposes mature creation, navigation, and clipboard workflows", () => {
  match(workspaceSource, /onOpenQuickAdd=\{tools\.openQuickAdd\}/);
  match(workspaceSource, /onOpenContextMenu=\{tools\.openContextMenu\}/);
  match(workspaceSource, /CanvasSelectionToolbar/);
  match(workspaceSource, /CanvasShortcutsDialog/);
  match(workspaceToolsSource, /serializeCanvasSubgraph/);
  match(workspaceToolsSource, /parseCanvasSubgraph/);
  match(
    workspaceToolsSource,
    /if \(typeof text === "string"\) \{\s*subgraph = parseCanvasSubgraph\(text\);\s*\}/,
  );
  doesNotMatch(workspaceToolsSource, /if \(parsed\) subgraph = parsed/);
  match(workspaceToolsSource, /autoLayoutDag/);
  match(workspaceToolsSource, /connectDraftToNewNode/);
  match(commandMenuSource, /role="combobox"/);
  match(commandMenuSource, /ArrowDown/);
  match(viewportControlsSource, /当前缩放比例/);
  match(clipboardSource, /CANVAS_CLIPBOARD_PREFIX/);
});

test("canvas commands track the store primary selection independently of selection order", () => {
  match(
    workspaceToolsSource,
    /useStore\(store, \(state\) => state\.selectedNodeId\)/,
  );
  match(workspaceToolsSource, /selectedNodeId,\s*selectedCount/);
  doesNotMatch(workspaceToolsSource, /node\.id === selectedNodeIds\[0\]/);
});

test("canvas catalog creation persists presets and filters quick connections by real ports", () => {
  match(paletteSource, /CANVAS_NODE_CATALOG/);
  match(paletteSource, /role="tablist"/);
  match(paletteSource, /min-h-11/);
  match(paletteSource, /application\/lumen-canvas-node/);
  match(viewportSource, /findCanvasNodeCatalogItem/);
  match(viewportSource, /preset_id: catalogItem\.id/);
  match(workspaceToolsSource, /catalogAcceptsConnection/);
  match(workspaceToolsSource, /createCanvasNodeFromCatalog/);
  match(workspaceToolsSource, /validateCanvasConnection\(candidateGraph/);
});

test("canvas mask uploads request strict server preflight", () => {
  match(inspectorSource, /purpose: kind === "mask" \? "inpaint_mask"/);
  match(
    nodeConfigEditorSource,
    /accept=\{isMask \? "image\/png" : "image\/png,image\/jpeg,image\/webp"\}/,
  );
});

test("canvas stale uploads and output selections are fenced", () => {
  match(inspectorSource, /cleanupStaleCanvasAsset/);
  match(inspectorSource, /shouldCleanupStaleCanvasAsset/);
  match(inspectorSource, /asset\.id,\s*asset\.created,\s*request\.initialAssetId/);
  match(inspectorSource, /createdByRequest &&\s*assetId\.trim\(\)\.length > 0/);
  match(inspectorSource, /deleteCanvasUploadedAsset/);
  match(apiSource, /selection_revision: selectionRevision/);
  match(querySource, /queueRef = useRef\(new Map<string, Promise<void>>\(\)\)/);
  match(querySource, /revisionRef = useRef\(new Map<string, number>\(\)\)/);
  match(querySource, /previous\.catch\(\(\) => undefined\)/);
});

test("canvas video auto selection aggregates compatible capabilities", () => {
  doesNotMatch(nodeConfigEditorSource, /firstModelForAction/);
  match(nodeConfigEditorSource, /videoResolutionOptionsForModels/);
  match(nodeConfigEditorSource, /videoDurationOptionsForModels/);
  match(nodeConfigEditorSource, /selectVideoModelForParameters/);
});

test("canvas media and text editors preserve shared limits and fallbacks", () => {
  match(nodeConfigEditorSource, /output_format: value, background: "opaque"/);
  match(nodeConfigEditorSource, /CANVAS_NOTE_MAX_CHARS/);
  match(nodesSource, /CANVAS_NOTE_MAX_CHARS/);
  match(inspectorSource, /historyOutputPreviewSources/);
  match(inspectorSource, /sourceIndex \+ 1/);
  match(inspectorSource, /imageBinaryUrl/);
  match(constantsSource, /CANVAS_NOTE_MAX_CHARS = 20_000/);
  match(constantsSource, /normalizeCanvasNodeTitle/);
  match(inspectorSource, /normalizeCanvasNodeTitle/);
  match(nodesSource, /normalizeCanvasNodeTitle/);
});

test("image asset nodes accept direct paste, drop, and replacement uploads", () => {
  match(nodesSource, /CanvasImageAssetDropZone/);
  match(imageAssetDropSource, /data-canvas-image-dropzone/);
  match(imageAssetDropSource, /data-canvas-native-paste/);
  match(imageAssetDropSource, /onPaste=\{onPaste\}/);
  match(imageAssetDropSource, /onDrop=\{onDrop\}/);
  match(imageAssetDropSource, /uploadImage\(file/);
  match(imageAssetDropSource, /MAX_UPLOAD_SOURCE_BYTES/);
  match(workspaceInteractionsSource, /data-canvas-native-paste/);
});

test("canvas viewport and media rendering stay scalable and accessible", () => {
  match(viewportSource, /onlyRenderVisibleElements/);
  match(viewportSource, /useReducedMotion/);
  match(viewportSource, /COMPACT_MIN_ZOOM = 0\.08/);
  match(viewportSource, /ariaLabelConfig=\{CANVAS_ARIA_LABEL_CONFIG\}/);
  match(nodesSource, /imageVariantUrl\(output\.image_id, "display2048"\)/);
  match(nodesSource, /videoBinaryUrl\(output\.video_id\)/);
  match(nodesSource, /openLightboxFromItems/);
  match(nodesSource, /CanvasVideoPreviewDialog/);
  match(nodesSource, /CanvasOutputDownloadButton/);
  match(inspectorSource, /CanvasOutputDownloadButton/);
  match(videoPreviewSource, /CanvasOutputDownloadButton/);
  match(outputDownloadSource, /triggerImageDownload/);
  match(outputDownloadSource, /videoDownloadUrl/);
  match(outputDownloadSource, /data-canvas-output-download/);
  match(nodesSource, /output\.poster_url\?\.trim\(\)/);
  match(nodesSource, /loading="lazy"/);
  match(nodesSource, /decoding="async"/);
  match(nodesSource, /onCompleteConnection/);
  match(nodesSource, /tabIndex=\{onStartConnection \? 0 : -1\}/);
});

test("canvas connection targets are precomputed once and mobile focus uses node centers", () => {
  match(viewportSource, /buildConnectionCompatibility\(graph, connectionDraft\)/);
  match(viewportSource, /validateCanvasConnections\(graph, \[candidate\]\)/);
  match(viewportSource, /connectionCompatibility\.handlesByNode\.get\(node\.id\)/);
  match(viewportSource, /targets=\{connectionCompatibility\.targets\}/);
  match(viewportSource, /x: node\.position\.x \+ dimensions\.width \/ 2/);
  match(viewportSource, /y: node\.position\.y \+ dimensions\.height \/ 2/);
  doesNotMatch(viewportSource, /compatibleInputHandlesForNode/);
  doesNotMatch(viewportSource, /listCompatibleTargets/);
});

test("mobile canvas toolbar owns layout space instead of covering the viewport", () => {
  match(workspaceSource, /className="flex min-h-0 min-w-0 flex-col"/);
  match(
    mobileToolbarSource,
    /className="relative z-\[var\(--z-tabbar\)\] w-full shrink-0/,
  );
  doesNotMatch(mobileToolbarSource, /\babsolute\b/);
  doesNotMatch(mobileToolbarSource, /\bfixed\b/);
  doesNotMatch(viewportSource, /canvas-mobile-viewport-inset/);
});
