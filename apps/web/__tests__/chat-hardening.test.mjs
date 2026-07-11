import { ok, match, doesNotMatch } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, "..");

function source(path) {
  return readFileSync(join(webRoot, path), "utf8");
}

await import("../src/store/chat/types.test.ts");
await import("../src/store/chat/generationSlice.test.ts");
await import("../src/store/chat/history.test.ts");
await import("../src/store/chat/composerSlice.test.ts");
await import("../src/store/chat/taskRecovery.test.ts");
await import("../src/store/chat/storeReferences.test.ts");

test("chat store export is SSR-safe and browser-lazy", () => {
  const store = source("src/store/useChatStore.ts");

  match(store, /function getChatStore\(/);
  match(store, /typeof window === "undefined"/);
  match(store, /return createChatStore\(\);/);
  doesNotMatch(
    store,
    /export const useChatStore: ChatStoreHook = createChatStore\(\);/,
  );
});

test("completion stream patches are isolated and held until message lookup exists", () => {
  const store = source("src/store/useChatStore.ts");
  const streamPatches = source(
    "src/store/chat/completionStreamPatches.ts",
  );

  match(store, /const _completionMessageIds = new Map</);
  match(store, /COMPLETION_MESSAGE_ID_TTL_MS/);
  match(store, /const _pendingDeltasByCompletionId = new Map/);
  match(
    streamPatches,
    /if \(completionId\) return `comp:\$\{completionId\}`;/,
  );
  match(store, /COMPLETION_PENDING_DELTA_TTL_MS = 10_000/);
  match(store, /COMPLETION_PENDING_DELTA_MAX_ENTRIES = 1_000/);
  match(store, /setBounded\(\s*_pendingDeltasByCompletionId,/);
  match(store, /rememberCompletionMessage\(completionId, realAssistant\.id\);/);
});

test("composer snapshots and SSE payloads are locally hardened", () => {
  const store = source("src/store/useChatStore.ts");
  const history = source("src/store/chat/history.ts");
  const composer = source("src/store/chat/composerSlice.ts");
  const messageAdapters = source("src/store/chat/messageAdapters.ts");

  match(history, /export function clonePlainValue<T>\(value: T\): T/);
  match(composer, /attachments = clonePlainValue\(composer\.attachments\)/);
  match(
    composer,
    /attachments\.some\(\s*\(attachment\) =>\s*attachment\.id === composer\.mask\?\.target_attachment_id,?\s*\)/,
  );
  match(composer, /\? clonePlainValue\(composer\.mask\)[\s\S]*: null;/);
  match(store, /function ssePayloadRecord\(/);
  match(store, /dropped SSE event after store handler error/);
  match(messageAdapters, /normalizeCompletionToolStatus/);
  match(messageAdapters, /timed_out/);
  match(messageAdapters, /cancelled/);
  match(messageAdapters, /unknown/);
});

test("runtime upload limit falls back locally but accepts server defaults", () => {
  const limits = source("src/lib/uploadLimits.ts");
  const bootstrap = source("src/components/RuntimeDefaultsBootstrap.tsx");
  const layout = source("src/app/layout.tsx");
  const store = source("src/store/useChatStore.ts");

  match(limits, /FALLBACK_UPLOAD_SOURCE_BYTES = 50 \* 1024 \* 1024/);
  match(
    limits,
    /setMaxUploadSourceBytes\(bytes: number \| null \| undefined\)/,
  );
  match(bootstrap, /serverRuntimeDefaults\?\.upload_max_source_bytes/);
  match(layout, /import type \{ RuntimeDefaults \}/);
  match(
    layout,
    /function normalizeRuntimeDefaults\(value: unknown\): RuntimeDefaults/,
  );
  match(layout, /raw\.upload_max_source_bytes > 0/);
  match(store, /setMaxUploadSourceBytes\(defaults\.upload_max_source_bytes\)/);
});

test("authenticated bootstrap only clears chat identity for real logout states", () => {
  const bootstrap = source("src/components/RuntimeDefaultsBootstrap.tsx");

  match(bootstrap, /chatStore\.setCurrentUser\(meQuery\.data\.id\)/);
  doesNotMatch(bootstrap, /hydrateActiveTasks/);
  match(bootstrap, /useChatStore\.getState\(\)\.setCurrentUser\(null\)/);
  match(
    bootstrap,
    /error instanceof ApiError && error\.status === 401/,
  );
  match(
    bootstrap,
    /shouldClearChatIdentity\(isPublicAuthPath, meQuery\.error\)/,
  );
  doesNotMatch(bootstrap, /meQuery\.isError/);
});

test("upgrade and inpaint lazy UI keep visible recovery states", () => {
  const banner = source("src/components/SystemUpgradeBanner.tsx");
  const lazyInpaint = source("src/components/ui/inpaint/LazyInpaintModal.tsx");

  match(banner, /query\.state\.error\) return 30_000/);
  match(banner, /query\.state\.data\?\.running \? 5000 : 60000/);
  match(lazyInpaint, /loading: \(\) => \(/);
  match(lazyInpaint, /z-\[var\(--z-dialog\)\] bg-black\/60/);
});

test("image prewarm cache is bounded and concurrency-limited", () => {
  const preload = source("src/lib/imagePreload.ts");

  match(preload, /IMAGE_CACHE_LIMIT = 384/);
  match(preload, /VIDEO_CACHE_LIMIT = 96/);
  match(preload, /IMAGE_PREWARM_CONCURRENCY = 3/);
  match(preload, /VIDEO_PREWARM_CONCURRENCY = 1/);
  match(preload, /entry\.status === "fulfilled"/);
  match(preload, /map\.delete\(victim\)/);
});

test("SSE recovery requests are coalesced instead of dropped while busy", () => {
  const provider = source("src/components/SSEProvider.tsx");

  match(provider, /SSE_RECOVERY_COALESCE_MS = 600/);
  match(provider, /queuedRecoveryRef/);
  match(provider, /recoveryDisposedRef/);
  match(provider, /mergeRecoveryRequest/);
  match(
    provider,
    /hydrateTasks: current\.hydrateTasks \|\| next\.hydrateTasks/,
  );
});

test("composer pills abort prompt enhancement and guard duplicate submits", () => {
  const desktop = source(
    "src/components/ui/composer/desktop/DesktopComposerPill.tsx",
  );
  const mobile = source(
    "src/components/ui/composer/mobile/MobileComposerPill.tsx",
  );

  for (const composer of [desktop, mobile]) {
    match(
      composer,
      /const enhanceAbortRef = useRef<AbortController \| null>\(null\)/,
    );
    match(composer, /enhanceAbortRef\.current\?\.abort\(\)/);
    match(composer, /if \(ctl\.signal\.aborted\) return;/);
    match(composer, /const submittingRef = useRef\(false\)/);
    match(composer, /if \(submittingRef\.current\) return;/);
  }
});

test("api fetch preserves native abort semantics", () => {
  const http = source("src/lib/api/http.ts");

  match(http, /err instanceof Error && err\.name === "AbortError"/);
  match(http, /throw err;/);
});

test("chat reconciliation preserves terminal states and retry drafts", () => {
  const store = source("src/store/useChatStore.ts");
  const composer = source("src/store/chat/composerSlice.ts");
  const generationSlice = source("src/store/chat/generationSlice.ts");
  const history = source("src/store/chat/history.ts");
  const taskRecovery = source("src/store/chat/taskRecovery.ts");

  match(
    generationSlice,
    /!\["succeeded", "failed", "canceled"\]\.includes\(incomingStatus\)/,
  );
  match(
    taskRecovery,
    /mergeUnknownActiveGenerations\(\s*state\.generations,\s*incoming,/,
  );
  match(history, /preferredGenerationSnapshot\(existing, snapshot\)/);
  match(taskRecovery, /userSessionFence\.isCurrent\(userFence\)/);
  match(store, /isConversationMutationCurrent\(/);
  match(store, /restoreComposerOnFailure: false/);
  match(store, /isResetComposerDraft\(cur, retryComposer\) \|\| isRetryDraft/);
  match(
    store,
    /isResetComposerDraft\(cur, temporaryComposer\) \|\|\s*isTemporaryInpaintDraft/,
  );
  match(composer, /export function isResetComposerDraft\(/);
  match(composer, /export function isRetryComposerDraft\(/);
  match(composer, /export function isTemporaryInpaintComposerDraft\(/);
});

test("chat store delegates composer, task recovery, and pure reducer boundaries", () => {
  const store = source("src/store/useChatStore.ts");
  const composer = source("src/store/chat/composerSlice.ts");
  const imageParams = source("src/store/chat/imageParams.ts");
  const upload = source("src/store/chat/imageUpload.ts");
  const payload = source("src/store/chat/payload.ts");
  const messageAdapters = source("src/store/chat/messageAdapters.ts");
  const completionEvents = source("src/store/chat/completionEvents.ts");
  const reconciliation = source(
    "src/store/chat/messageReconciliation.ts",
  );
  const streamPatches = source(
    "src/store/chat/completionStreamPatches.ts",
  );
  const eviction = source("src/store/chat/base64Eviction.ts");
  const generationSlice = source("src/store/chat/generationSlice.ts");
  const history = source("src/store/chat/history.ts");
  const taskRecovery = source("src/store/chat/taskRecovery.ts");
  const types = source("src/store/chat/types.ts");

  match(store, /from "\.\/chat\/composerSlice"/);
  match(store, /from "\.\/chat\/imageParams"/);
  match(store, /from "\.\/chat\/imageUpload"/);
  match(store, /from "\.\/chat\/payload"/);
  match(store, /from "\.\/chat\/messageAdapters"/);
  match(store, /from "\.\/chat\/completionEvents"/);
  match(store, /from "\.\/chat\/messageReconciliation"/);
  match(store, /from "\.\/chat\/completionStreamPatches"/);
  match(store, /from "\.\/chat\/base64Eviction"/);
  match(store, /from "\.\/chat\/generationSlice"/);
  match(store, /from "\.\/chat\/history"/);
  match(store, /from "\.\/chat\/taskRecovery"/);
  match(store, /from "\.\/chat\/types"/);
  match(store, /export type \{ ReasoningEffort \} from "\.\/chat\/types"/);
  match(store, /\.\.\.createComposerActions\(set, get,/);
  match(store, /\.\.\.createTaskRecoveryActions\(set, get,/);
  match(composer, /export function createComposerActions\(/);
  match(taskRecovery, /export function createTaskRecoveryActions\(/);
  match(taskRecovery, /await get\(\)\.loadHistoricalMessages\(/);
  match(types, /export interface ChatState/);
  match(types, /export interface ComposerState/);
  match(generationSlice, /export function preferredGenerationSnapshot/);
  match(generationSlice, /export function mergeUnknownActiveGenerations/);
  match(history, /export function cloneConversationHistoryCacheEntry/);
  match(history, /export function makeConversationHistoryCacheEntry/);
  match(history, /export function buildMessageListState/);
  match(imageParams, /export function normalizeImageParams/);
  match(upload, /export async function compressToMaxDim/);
  match(payload, /export function billingMetaFromPayload/);
  for (const name of [
    "adaptBackendUserMessage",
    "coerceAssistantIntent",
    "optionalAssistantIntent",
    "coerceAssistantStatus",
    "normalizeCompletionToolStatus",
    "coerceCompletionToolCalls",
    "coerceMemoryWrites",
    "coerceUsedMemorySummary",
    "mergeCompletionToolCall",
    "adaptBackendAssistantMessage",
  ]) {
    match(messageAdapters, new RegExp(`export function ${name}\\(`));
    doesNotMatch(store, new RegExp(`function ${name}\\(`));
  }
  for (const name of [
    "completionMessageMatches",
    "applyCompletionProgressEvent",
    "applyCompletionSucceededEvent",
    "applyCompletionLifecycleEvent",
    "applyCompletionEventToMessage",
  ]) {
    match(completionEvents, new RegExp(`export function ${name}\\(`));
    doesNotMatch(store, new RegExp(`function ${name}\\(`));
  }
  match(completionEvents, /export type SseIdGetter/);
  doesNotMatch(store, /^type SseIdGetter/m);
  match(reconciliation, /export function applyCompletionSnapshot/);
  match(streamPatches, /export function applyCompletionStreamPatches/);
  match(eviction, /export function buildBase64EvictionPatch/);
  doesNotMatch(store, /function normalizeImageParams/);
  doesNotMatch(store, /async function compressToMaxDim/);
  doesNotMatch(store, /function billingMetaFromPayload/);
  doesNotMatch(store, /function applyCompletionSnapshot/);
  doesNotMatch(store, /function completionStreamPatchKey/);
  doesNotMatch(store, /function evictGenerationImages/);
  doesNotMatch(store, /function generationExplainabilityFromBackend/);
  doesNotMatch(store, /function preferredGenerationSnapshot/);
  doesNotMatch(store, /function buildMessageListState/);
  doesNotMatch(store, /function cloneComposerState/);
  doesNotMatch(store, /function selectInflightTaskChecks/);
  doesNotMatch(store, /async function pollGenerationTask/);
  doesNotMatch(store, /async function pollCompletionTask/);
  for (const extracted of [
    types,
    composer,
    generationSlice,
    history,
    taskRecovery,
  ]) {
    doesNotMatch(extracted, /useChatStore/);
  }
  const storeLoc = store.trimEnd().split(/\r?\n/).length;
  ok(storeLoc < 3000, `useChatStore.ts must stay below 3000 LOC, got ${storeLoc}`);
});

test("core async actions, runtime registries, SSE handler, and singleton remain store-owned", () => {
  const store = source("src/store/useChatStore.ts");
  const composer = source("src/store/chat/composerSlice.ts");
  const generationSlice = source("src/store/chat/generationSlice.ts");
  const history = source("src/store/chat/history.ts");
  const taskRecovery = source("src/store/chat/taskRecovery.ts");

  for (const action of [
    "sendMessage",
    "retryAssistant",
    "retryGeneration",
    "regenerateAssistant",
    "upscaleImage",
    "rerollImage",
    "submitInpaintTask",
    "applySSEEvent",
  ]) {
    const actionPattern = new RegExp(
      `(?:async\\s+)?${action}(?:\\s*:\\s*)?\\(`,
    );
    match(store, actionPattern);
    doesNotMatch(generationSlice, actionPattern);
    doesNotMatch(history, actionPattern);
  }
  for (const action of [
    "setText",
    "addAttachment",
    "clearComposer",
    "promoteImageToReference",
  ]) {
    match(composer, new RegExp(`${action}:\\s*`));
  }
  for (const action of [
    "refreshCompletionText",
    "pollInflightTasks",
    "hydrateActiveTasks",
  ]) {
    match(taskRecovery, new RegExp(`async ${action}\\(`));
  }
  match(store, /const _conversationHistoryCache = new Map/);
  match(store, /const _generationIdAliases = new Map/);
  match(store, /let _base64EvictionTimer:/);
  match(store, /let _completionStreamTimer:/);
  match(store, /function createChatStore\(/);
  match(store, /export const useChatStore:/);
});

test("desktop and mobile canvases share generation render signatures", () => {
  const shared = source("src/components/ui/chat/generationRenderSignature.ts");
  const desktop = source(
    "src/components/ui/chat/desktop/DesktopConversationCanvas.tsx",
  );
  const mobile = source(
    "src/components/ui/chat/mobile/MobileConversationCanvas.tsx",
  );

  match(shared, /export function generationRenderSignature/);
  match(desktop, /from "@\/components\/ui\/chat\/generationRenderSignature"/);
  match(mobile, /from "@\/components\/ui\/chat\/generationRenderSignature"/);
  doesNotMatch(desktop, /function generationRenderSignature/);
  doesNotMatch(mobile, /function generationRenderSignature/);
});

test("settings saves only clear the submitted revision", () => {
  const settings = source("src/app/admin/_panels/SettingsPanel.tsx");

  match(settings, /const submittedOps = ops/);
  match(settings, /clearSubmittedOps\(currentOps, submittedOps\)/);
  match(settings, /current\.value === submitted\.value/);
});

test("desktop image composer keeps high-frequency settings inline", () => {
  const desktop = source(
    "src/components/ui/composer/desktop/DesktopComposerPill.tsx",
  );
  const quickSettings = source(
    "src/components/ui/composer/desktop/DesktopComposerExecutionControls.tsx",
  );

  match(desktop, /<ComposerExecutionControls/);
  match(quickSettings, /function ImageQuickSettingsBar/);
  match(quickSettings, /ariaLabel="生成数量"/);
  match(quickSettings, /aria-label="宽高比"/);
  match(quickSettings, /aria-haspopup="dialog"/);
  match(quickSettings, /<AspectRatioPicker/);
  match(quickSettings, /ariaLabel="输出尺寸"/);
  match(quickSettings, /ariaLabel="生成质量"/);
  match(quickSettings, /aria-label=\{fast \? "关闭 Fast" : "开启 Fast"\}/);
});

test("desktop studio uses one compact control family and aligned content rails", () => {
  const segmented = source(
    "src/components/ui/primitives/mobile/SegmentedControl.tsx",
  );
  const contextBar = source("src/components/ui/shell/StudioContextBar.tsx");
  const composer = source(
    "src/components/ui/composer/desktop/DesktopComposerPill.tsx",
  );
  const canvas = source(
    "src/components/ui/chat/desktop/DesktopConversationCanvas.tsx",
  );
  const divider = source(
    "src/components/ui/chat/desktop/DesktopSceneDivider.tsx",
  );

  match(segmented, /density\?: "default" \| "compact"/);
  match(segmented, /aria-orientation="horizontal"/);
  match(segmented, /tabIndex=\{index === tabStopIndex \? 0 : -1\}/);
  match(segmented, /case "ArrowRight":/);
  match(segmented, /case "Home":/);
  match(segmented, /focus-visible:shadow-\[var\(--ring\)\]/);
  match(contextBar, /density="compact"/);
  match(contextBar, /<span>图库<\/span>/);
  match(composer, /density="compact"/);
  match(canvas, /max-w-\[var\(--content-composer\)\]/);
  match(canvas, /aria-label="回到最新"/);
  match(canvas, /var\(--studio-sidebar-offset, 0px\) \/ 2/);
  match(divider, /max-w-\[var\(--content-composer\)\]/);
});

test("completion status line renders new tool terminal states", () => {
  const statusLine = source("src/components/ui/chat/CompletionStatusLine.tsx");

  match(statusLine, /timed_out/);
  match(statusLine, /cancelled/);
  match(statusLine, /unknown/);
  ok(statusLine.includes("状态未知"));
});
