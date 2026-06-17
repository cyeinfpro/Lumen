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

test("chat store export is SSR-safe and browser-lazy", () => {
  const store = source("src/store/useChatStore.ts");

  match(store, /function getChatStore\(/);
  match(store, /typeof window === "undefined"/);
  match(store, /return createChatStore\(\);/);
  doesNotMatch(store, /export const useChatStore: ChatStoreHook = createChatStore\(\);/);
});

test("completion stream patches are isolated and held until message lookup exists", () => {
  const store = source("src/store/useChatStore.ts");

  match(store, /const _completionMessageIds = new Map</);
  match(store, /COMPLETION_MESSAGE_ID_TTL_MS/);
  match(store, /const _pendingDeltasByCompletionId = new Map/);
  match(store, /if \(compId\) return `comp:\$\{compId\}`;/);
  match(store, /COMPLETION_PENDING_DELTA_TTL_MS = 10_000/);
  match(store, /COMPLETION_PENDING_DELTA_MAX_ENTRIES = 1_000/);
  match(store, /setBounded\(\s*_pendingDeltasByCompletionId,/);
  match(store, /rememberCompletionMessage\(completionId, realAssistant\.id\);/);
});

test("composer snapshots and SSE payloads are locally hardened", () => {
  const store = source("src/store/useChatStore.ts");

  match(store, /function clonePlainValue<T>\(value: T\): T/);
  match(store, /attachments = clonePlainValue\(composer\.attachments\)/);
  match(store, /attachments\.some\(\(attachment\) => attachment\.id === composer\.mask\?\.target_attachment_id\)/);
  match(store, /\? clonePlainValue\(composer\.mask\)[\s\S]*: null;/);
  match(store, /function ssePayloadRecord\(/);
  match(store, /dropped SSE event after store handler error/);
  match(store, /normalizeCompletionToolStatus/);
  match(store, /timed_out/);
  match(store, /cancelled/);
  match(store, /unknown/);
});

test("runtime upload limit falls back locally but accepts server defaults", () => {
  const limits = source("src/lib/uploadLimits.ts");
  const bootstrap = source("src/components/RuntimeDefaultsBootstrap.tsx");
  const layout = source("src/app/layout.tsx");
  const store = source("src/store/useChatStore.ts");

  match(limits, /FALLBACK_UPLOAD_SOURCE_BYTES = 50 \* 1024 \* 1024/);
  match(limits, /setMaxUploadSourceBytes\(bytes: number \| null \| undefined\)/);
  match(bootstrap, /serverRuntimeDefaults\?\.upload_max_source_bytes/);
  match(layout, /import type \{ RuntimeDefaults \}/);
  match(layout, /function normalizeRuntimeDefaults\(value: unknown\): RuntimeDefaults/);
  match(layout, /raw\.upload_max_source_bytes > 0/);
  match(store, /setMaxUploadSourceBytes\(defaults\.upload_max_source_bytes\)/);
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
  match(provider, /hydrateTasks: current\.hydrateTasks \|\| next\.hydrateTasks/);
});

test("composer pills abort prompt enhancement and guard duplicate submits", () => {
  const desktop = source("src/components/ui/composer/desktop/DesktopComposerPill.tsx");
  const mobile = source("src/components/ui/composer/mobile/MobileComposerPill.tsx");

  for (const composer of [desktop, mobile]) {
    match(composer, /const enhanceAbortRef = useRef<AbortController \| null>\(null\)/);
    match(composer, /enhanceAbortRef\.current\?\.abort\(\)/);
    match(composer, /if \(ctl\.signal\.aborted\) return;/);
    match(composer, /const submittingRef = useRef\(false\)/);
    match(composer, /if \(submittingRef\.current\) return;/);
  }
});

test("desktop web build uses runtime env without unsupported config flag", () => {
  const buildDesktop = source("scripts/build-desktop.mjs");
  const nextConfig = source("next.config.ts");

  match(buildDesktop, /NEXT_PUBLIC_LUMEN_RUNTIME = "desktop"/);
  match(buildDesktop, /\[nextCli, "build"\]/);
  doesNotMatch(buildDesktop, /"--config"/);
  match(nextConfig, /isDesktopRuntime = process\.env\.NEXT_PUBLIC_LUMEN_RUNTIME === "desktop"/);
  match(nextConfig, /unoptimized: isDesktopRuntime/);
});

test("completion status line renders new tool terminal states", () => {
  const statusLine = source("src/components/ui/chat/CompletionStatusLine.tsx");

  match(statusLine, /timed_out/);
  match(statusLine, /cancelled/);
  match(statusLine, /unknown/);
  ok(statusLine.includes("状态未知"));
});
