import { doesNotMatch, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, "..");

function source(path) {
  return readFileSync(join(webRoot, path), "utf8");
}

test("sidebar controls each rendered shell from its own DOM context", () => {
  const sidebar = source("src/components/ui/Sidebar.tsx");

  match(sidebar, /data-sidebar-archive-menu/);
  match(sidebar, /target\.closest\("\[data-sidebar-archive-menu\]"\)/);
  match(sidebar, /data-sidebar-scroll/);
  match(sidebar, /e\.currentTarget\.querySelectorAll<HTMLElement>/);
  match(
    sidebar,
    /virtualRootRef\.current\?\.closest<HTMLDivElement>\(\s*"\[data-sidebar-scroll\]"/,
  );
  doesNotMatch(sidebar, /const listRef =/);
  doesNotMatch(sidebar, /const archiveMenuRef =/);
  doesNotMatch(sidebar, /const rowsCacheRef =/);
});

test("error boundary clears its error before returning home", () => {
  const boundary = source("src/components/ErrorBoundary.tsx");

  match(
    boundary,
    /<Link\s+href="\/"\s+onClick=\{this\.handleReset\}/,
  );
  match(
    boundary,
    /handleReset = \(\): void => \{\s*this\.setState\(\{ hasError: false, error: null \}\);/,
  );
});

test("service worker updates never force-refresh active work", () => {
  const register = source("src/components/ServiceWorkerRegister.tsx");

  match(register, /reg\.update\(\)\.catch/);
  match(register, /worker\.postMessage\(\{ type: "SKIP_WAITING" \}\)/);
  doesNotMatch(register, /window\.location\.reload/);
  doesNotMatch(register, /["']controllerchange["']/);
});

test("archived sidebar rows invalidate and remeasure layout caches", () => {
  const sidebar = source("src/components/ui/Sidebar.tsx");

  match(sidebar, /const layoutKey = useMemo\(/);
  match(
    sidebar,
    /items\.map\(\(conv\) => \[conv\.id, titleOf\(conv\), conv\.archived\]\)/,
  );
  match(sidebar, /getItemKey: \(index\) => items\[index\]\?\.id \?\? index/);
  match(
    sidebar,
    /useEffect\(\(\) => \{\s*if \(!shouldVirtualize\) return;\s*rowVirtualizer\.measure\(\);/,
  );
  match(sidebar, /ref=\{rowVirtualizer\.measureElement\}/);
  match(sidebar, /translateY\(\$\{virtualRow\.start\}px\)/);
});

test("system prompt manager fetches the exact current conversation", () => {
  const manager = source("src/components/ui/SystemPromptManager.tsx");

  match(
    manager,
    /function useCurrentConversationQuery\(currentConvId: string \| null\)/,
  );
  match(
    manager,
    /queryKey: qk\.user\(userScope\.userId\)\.conversationDetail\(conversationId\)/,
  );
  match(manager, /queryFn: \(\) => getConversation\(conversationId\)/);
  match(manager, /enabled: userScope\.enabled && Boolean\(currentConvId\)/);
  doesNotMatch(manager, /queryKey: \["conversations", "detail"/);
  doesNotMatch(manager, /useListConversationsQuery/);
  doesNotMatch(manager, /limit:\s*100/);
});

test("collapsed desktop sidebar is removed from focus navigation", () => {
  const sidebar = source("src/components/ui/Sidebar.tsx");

  match(
    sidebar,
    /<aside\s+\{\.\.\.ariaCommon\}\s+aria-hidden=\{!sidebarOpen\}\s+inert=\{!sidebarOpen \? true : undefined\}/,
  );
});

test("SSE replay truncation advances the reconnect cursor before closing", () => {
  const useSSE = source("src/lib/useSSE.ts");

  match(useSSE, /INTERNAL_SSE_EVENT_NAMES = new Set\(\["replay_truncated"\]\)/);
  match(
    useSSE,
    /const eventNames = new Set<string>\(INTERNAL_SSE_EVENT_NAMES\)/,
  );
  match(
    useSSE,
    /if \(INTERNAL_SSE_EVENT_NAMES\.has\(name\)\) \{\s*if \(ev\.lastEventId\) this\.lastEventId = ev\.lastEventId;\s*return;\s*\}\s*this\.dispatchNamed\(ev\);/,
  );
});
