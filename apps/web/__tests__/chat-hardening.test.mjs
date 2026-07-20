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
  const runtime = source("src/store/chat/runtime.ts");
  const streamPatches = source(
    "src/store/chat/completionStreamPatches.ts",
  );

  match(runtime, /const _completionMessageIds = new Map</);
  match(runtime, /COMPLETION_MESSAGE_ID_TTL_MS/);
  match(runtime, /const _pendingDeltasByCompletionId = new Map/);
  match(
    streamPatches,
    /if \(completionId\) return `comp:\$\{completionId\}`;/,
  );
  match(runtime, /COMPLETION_PENDING_DELTA_TTL_MS = 10_000/);
  match(runtime, /COMPLETION_PENDING_DELTA_MAX_ENTRIES = 1_000/);
  match(runtime, /setBounded\(\s*_pendingDeltasByCompletionId,/);
  match(store, /rememberCompletionMessage\(completionId, realAssistant\.id\);/);
});

test("composer snapshots and SSE payloads are locally hardened", () => {
  const store = source("src/store/useChatStore.ts");
  const runtime = source("src/store/chat/runtime.ts");
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
  match(runtime, /function ssePayloadRecord\(/);
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

test("authenticated bootstrap delegates fail-closed identity recovery", () => {
  const bootstrap = source("src/components/RuntimeDefaultsBootstrap.tsx");
  const identityRecovery = source(
    "src/components/useIdentityRevalidation.ts",
  );

  match(bootstrap, /useIdentityRevalidation\(\{/);
  match(bootstrap, /setCurrentUser\(meQuery\.data\.id\)/);
  doesNotMatch(bootstrap, /hydrateActiveTasks/);
  match(
    identityRecovery,
    /prepareUserIdentityRevalidation\(queryClient, state\.retainedUserId\)/,
  );
  match(
    identityRecovery,
    /isUnauthorizedIdentityError\(error\)/,
  );
  match(identityRecovery, /scheduleRetry\(\)/);
  match(identityRecovery, /setCurrentUser\(null\)/);
  doesNotMatch(identityRecovery, /query\.isError/);
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

test("shared prompt enhancement aborts stale work and composer pills guard duplicate submits", () => {
  const desktop = source(
    "src/components/ui/composer/desktop/DesktopComposerPill.tsx",
  );
  const mobile = source(
    "src/components/ui/composer/mobile/MobileComposerPill.tsx",
  );
  const enhancement = source(
    "src/components/ui/composer/shared/PromptEnhancementCandidate.tsx",
  );

  match(enhancement, /const abortRef = useRef<AbortController \| null>\(null\)/);
  match(enhancement, /abortRef\.current\?\.abort\(\)/);
  match(enhancement, /controller\.signal\.aborted/);
  match(enhancement, /onApply\(candidate\)/);

  for (const composer of [desktop, mobile]) {
    match(composer, /usePromptEnhancementCandidate/);
    match(composer, /const submittingRef = useRef\(false\)/);
    match(composer, /if \(submittingRef\.current\) return;/);
  }
});

test("api fetch preserves native abort semantics", () => {
  const http = source("src/lib/api/http.ts");

  match(http, /err instanceof Error && err\.name === "AbortError"/);
  match(http, /throw err;/);
  match(
    http,
    /parseApiError\(res\.status, data\)\.code === CSRF_FAILED_CODE/,
  );
});

test("chat reconciliation preserves terminal states and retry drafts", () => {
  const runtime = source("src/store/chat/runtime.ts");
  const generationActions = source("src/store/chat/generationActions.ts");
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
  match(runtime, /isConversationMutationCurrent\(/);
  match(generationActions, /restoreComposerOnFailure: false/);
  match(
    generationActions,
    /isResetComposerDraft\(cur, retryComposer\) \|\| isRetryDraft/,
  );
  match(
    generationActions,
    /isResetComposerDraft\(cur, temporaryComposer\) \|\|\s*isTemporaryInpaintDraft/,
  );
  match(composer, /export function isResetComposerDraft\(/);
  match(composer, /export function isRetryComposerDraft\(/);
  match(composer, /export function isTemporaryInpaintComposerDraft\(/);
});

test("chat store delegates composer, task recovery, and pure reducer boundaries", () => {
  const store = source("src/store/useChatStore.ts");
  const runtime = source("src/store/chat/runtime.ts");
  const conversationActions = source(
    "src/store/chat/conversationActions.ts",
  );
  const generationActions = source("src/store/chat/generationActions.ts");
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
  match(conversationActions, /from "\.\/imageUpload"/);
  match(store, /from "\.\/chat\/payload"/);
  match(store, /from "\.\/chat\/messageAdapters"/);
  match(store, /from "\.\/chat\/completionEvents"/);
  match(store, /from "\.\/chat\/messageReconciliation"/);
  match(runtime, /from "\.\/completionStreamPatches"/);
  match(runtime, /from "\.\/base64Eviction"/);
  match(store, /from "\.\/chat\/generationSlice"/);
  match(store, /from "\.\/chat\/history"/);
  match(store, /from "\.\/chat\/taskRecovery"/);
  match(store, /from "\.\/chat\/types"/);
  match(store, /from "\.\/chat\/runtime"/);
  match(store, /from "\.\/chat\/conversationActions"/);
  match(store, /from "\.\/chat\/generationActions"/);
  match(store, /export type \{ ReasoningEffort \} from "\.\/chat\/types"/);
  match(store, /\.\.\.createComposerActions\(set, get,/);
  match(store, /\.\.\.createConversationActions\(set, get\)/);
  match(store, /\.\.\.createGenerationActions\(set, get,/);
  match(store, /\.\.\.createTaskRecoveryActions\(set, get,/);
  match(composer, /export function createComposerActions\(/);
  match(
    conversationActions,
    /export function createConversationActions\(/,
  );
  match(generationActions, /export function createGenerationActions\(/);
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
  ok(storeLoc < 1500, `useChatStore.ts must stay below 1500 LOC, got ${storeLoc}`);
});

test("core async actions, runtime registries, SSE handler, and singleton remain store-owned", () => {
  const store = source("src/store/useChatStore.ts");
  const generationActions = source("src/store/chat/generationActions.ts");
  const runtime = source("src/store/chat/runtime.ts");
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
    match(
      ["sendMessage", "applySSEEvent"].includes(action)
        ? store
        : generationActions,
      actionPattern,
    );
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
  match(runtime, /const _conversationHistoryCache = new Map/);
  match(runtime, /const _generationIdAliases = new Map/);
  match(runtime, /let _base64EvictionTimer:/);
  match(runtime, /let _completionStreamTimer:/);
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

test("mobile image composer exposes complete quick settings and advanced access", () => {
  const mobile = source(
    "src/components/ui/composer/mobile/MobileComposerPill.tsx",
  );
  const quickSettings = source(
    "src/components/ui/composer/mobile/MobileComposerExecutionControls.tsx",
  );

  match(mobile, /<MobileComposerExecutionControls/);
  match(quickSettings, /function MobileImageQuickSettingsBar/);
  match(quickSettings, /ariaLabel="生成数量"/);
  match(quickSettings, /COUNT_OPTIONS = \[1, 2, 3, 4, 5, 6, 7, 8, 9, 10\]/);
  match(quickSettings, /aria-label=\{`宽高比 \$\{aspect\}`\}/);
  match(quickSettings, /aria-haspopup="dialog"/);
  match(quickSettings, /ariaLabel="输出尺寸"/);
  match(quickSettings, /ariaLabel="生成质量"/);
  match(quickSettings, /aria-label=\{fast \? "关闭 Fast" : "开启 Fast"\}/);
  match(quickSettings, /aria-label="更多执行设置"/);
  match(quickSettings, /onClick=\{onAdjust\}/);
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
