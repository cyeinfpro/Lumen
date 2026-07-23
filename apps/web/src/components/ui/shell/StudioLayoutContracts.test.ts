import { deepEqual, doesNotMatch, equal, match, ok } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import * as ts from "typescript";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const pageSource = source("../../../app/page.tsx");
const responsiveSource = source("./ResponsiveStudio.tsx");
const desktopNavSource = source("./DesktopTopNav.tsx");
const desktopStudioSource = source("./DesktopStudio.tsx");
const mobileStudioSource = source("./MobileStudio.tsx");
const conversationRouteSyncSource = source("./useConversationRouteSync.ts");
const conversationSelectionSource = source("./conversationSelection.ts");
const defaultConversationSelectionSource = source(
  "./useDefaultConversationSelection.ts",
);
const conversationListSource = source("../me/ConversationList.tsx");
const mobileTopBarSource = source("./MobileStudioTopBar.tsx");
const mobileTabBarSource = source("./MobileTabBar.tsx");
const mobileMeSource = source("./MobileMe.tsx");
const mobileStreamSource = source("./MobileStream.tsx");
const settingsShellSource = source("./SettingsShell.tsx");
const mobileDrawerSource = source("./MobileConversationDrawer.tsx");
const sidebarSource = source("../Sidebar.tsx");
const mobileCanvasSource = source("../chat/mobile/MobileConversationCanvas.tsx");
const generationTileSource = source("../stream/GenerationTile.tsx");
const mobileComposerSource = source("../composer/mobile/MobileComposerPill.tsx");
const streamSearchSource = source("../stream/StreamSearchBar.tsx");
const viewportSource = source("../../../hooks/useKeyboardInset.ts");
const mediaQuerySource = source("../../../hooks/useMediaQuery.ts");
const inputSource = source("../primitives/Input.tsx");
const textareaSource = source("../primitives/Textarea.tsx");
const globalsSource = source("../../../app/globals.css");

type ScrollToGate = {
  targetId: string;
  locatedAtMessageCount: number;
  resumed: boolean;
} | null;

type ScrollToGateResult = {
  next: ScrollToGate;
  suppress: boolean;
  forceResume: boolean;
};

function loadScrollToGate() {
  const start = mobileStudioSource.indexOf("type ScrollToAutoScrollGate");
  const end = mobileStudioSource.indexOf("export function MobileStudio");
  ok(start >= 0 && end > start, "missing scrollTo gate helper");
  const output = ts.transpileModule(
    `${mobileStudioSource.slice(start, end)}
export { nextScrollToAutoScrollGate };`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRecord = {
    exports: {} as {
      nextScrollToAutoScrollGate: (input: {
        current: ScrollToGate;
        targetId: string | null;
        targetReady: boolean;
        messageCount: number;
      }) => ScrollToGateResult;
    },
  };
  runInNewContext(output, {
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return moduleRecord.exports.nextScrollToAutoScrollGate;
}

function loadFirstActiveConversation() {
  const output = ts.transpileModule(
    `${conversationSelectionSource}
module.exports.firstActiveConversation = firstActiveConversation;`,
    {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    },
  ).outputText;
  const moduleRecord = {
    exports: {} as {
      firstActiveConversation: <T extends { archived: boolean }>(
        conversations: readonly T[],
      ) => T | null;
    },
  };
  runInNewContext(output, {
    module: moduleRecord,
    exports: moduleRecord.exports,
  });
  return moduleRecord.exports.firstActiveConversation;
}

function plainValue<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function cssBlock(selector: string): string {
  const start = globalsSource.indexOf(selector);
  ok(start >= 0, `missing CSS block ${selector}`);
  const open = globalsSource.indexOf("{", start);
  const close = globalsSource.indexOf("\n  }", open);
  ok(open >= 0 && close >= 0, `invalid CSS block ${selector}`);
  return globalsSource.slice(open + 1, close);
}

function cssHex(block: string, token: string): string {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const value = new RegExp(`${escaped}:\\s*(#[0-9A-Fa-f]{6})`).exec(block)?.[1];
  ok(value, `missing ${token}`);
  return value;
}

function relativeLuminance(hex: string): number {
  const channels = hex
    .slice(1)
    .match(/.{2}/g)
    ?.map((value) => Number.parseInt(value, 16) / 255);
  ok(channels?.length === 3, `invalid color ${hex}`);
  const linear = channels.map((value) =>
    value <= 0.04045
      ? value / 12.92
      : ((value + 0.055) / 1.055) ** 2.4,
  );
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}

function contrastRatio(foreground: string, background: string): number {
  const foregroundLuminance = relativeLuminance(foreground);
  const backgroundLuminance = relativeLuminance(background);
  const lighter = Math.max(foregroundLuminance, backgroundLuminance);
  const darker = Math.min(foregroundLuminance, backgroundLuminance);
  return (lighter + 0.05) / (darker + 0.05);
}

test("studio renders a real server-selected shell without ssr:false", () => {
  match(pageSource, /<ResponsiveStudio initialMobile=\{initialMobile\} \/>/);
  match(responsiveSource, /detectedMobile \?\? initialMobile/);
  doesNotMatch(pageSource, /next\/dynamic|ssr:\s*false|ShellSkeleton/);
});

test("root studio opens the latest active conversation by default", () => {
  const firstActiveConversation = loadFirstActiveConversation();
  equal(
    firstActiveConversation([
      { id: "latest", archived: false },
      { id: "archived", archived: true },
      { id: "older", archived: false },
    ])?.id,
    "latest",
  );
  match(desktopStudioSource, /useDefaultConversationSelection\(/);
  match(mobileStudioSource, /useDefaultConversationSelection\(/);
  match(
    defaultConversationSelectionSource,
    /if \(currentConvId \|\| urlConversationId\) return/,
  );
  match(defaultConversationSelectionSource, /setCurrentConv\(first\.id\)/);
  match(
    defaultConversationSelectionSource,
    /loadHistoricalMessages\(first\.id\)/,
  );
  doesNotMatch(desktopStudioSource, /rootStartsNew/);
  doesNotMatch(mobileStudioSource, /rootStartsNew/);
});

test("conversation selection updates the URL without resetting or refreshing the studio", () => {
  match(
    conversationRouteSyncSource,
    /window\.history\.replaceState\(window\.history\.state, "", href\)/,
  );
  doesNotMatch(conversationRouteSyncSource, /useRouter|router\.replace/);
  doesNotMatch(conversationRouteSyncSource, /rootStartsNew|setCurrentConv\(null\)/);
  match(
    conversationListSource,
    /router\.push\(`\/\?conversationId=\$\{encodeURIComponent\(conv\.id\)\}`\)/,
  );
  doesNotMatch(conversationListSource, /loadHistoricalMessages/);
});

test("desktop primary navigation is viewport-centered and uses links", () => {
  match(
    desktopNavSource,
    /grid-cols-\[minmax\(0,1fr\)_auto_minmax\(0,1fr\)\]/,
  );
  match(desktopNavSource, /data-testid="desktop-primary-nav"/);
  match(desktopNavSource, /<Link[\s\S]*href=\{tab\.route\}/);
  doesNotMatch(desktopNavSource, /MoreNavigationMenu|compactOverflowItems/);
  doesNotMatch(desktopNavSource, /router\.push|justify-center overflow-hidden/);
});

test("desktop drawer traps focus and restores the trigger", () => {
  match(desktopStudioSource, /background\.inert = true/);
  match(desktopStudioSource, /e\.key !== "Tab"/);
  match(desktopStudioSource, /returnFocusTarget\?\.focus\(\)/);
  match(desktopStudioSource, /document\.body\.style\.overflow = "hidden"/);
  match(desktopStudioSource, /previousBackgroundInert/);
  match(desktopStudioSource, /previousBackgroundAriaHidden/);
});

test("responsive shell persists the first measured viewport", () => {
  match(mediaQuerySource, /function syncMediaQuerySnapshot/);
  match(
    mediaQuerySource,
    /syncMediaQuerySnapshot\(query, readMediaQuery\(query\)\)/,
  );
  match(mediaQuerySource, /syncMediaQuerySnapshot\(query, mql\.matches\)/);
});

test("mobile bottom stack includes the measured task island", () => {
  match(mobileStudioSource, /useElementBlockSize<HTMLDivElement>/);
  match(
    mobileStudioSource,
    /--bottom-overlay-stack/,
  );
  match(mobileStudioSource, /paddingBottom: "var\(--bottom-overlay-stack\)"/);
  match(mobileStudioSource, /data-testid="conversation-scroll"/);
  match(mobileCanvasSource, /var\(--bottom-overlay-stack, 120px\)/);
});

test("empty mobile studio starts at the top instead of auto-scrolling", () => {
  match(mobileStudioSource, /if \(messages\.length === 0\)/);
  match(
    mobileStudioSource,
    /el\.scrollTo\(\{ top: 0, behavior: "auto" \}\)/,
  );
});

test("mobile scrollTo suppresses location once and resumes for the next message", () => {
  const nextGate = loadScrollToGate();

  deepEqual(
    plainValue(nextGate({
      current: null,
      targetId: "message-4",
      targetReady: false,
      messageCount: 0,
    })),
    { next: null, suppress: true, forceResume: false },
  );

  const located = nextGate({
    current: null,
    targetId: "message-4",
    targetReady: true,
    messageCount: 4,
  });
  deepEqual(plainValue(located), {
    next: {
      targetId: "message-4",
      locatedAtMessageCount: 4,
      resumed: false,
    },
    suppress: true,
    forceResume: false,
  });

  const resumed = nextGate({
    current: located.next,
    targetId: "message-4",
    targetReady: true,
    messageCount: 5,
  });
  deepEqual(plainValue(resumed), {
    next: {
      targetId: "message-4",
      locatedAtMessageCount: 4,
      resumed: true,
    },
    suppress: false,
    forceResume: true,
  });
  deepEqual(
    plainValue(nextGate({
      current: resumed.next,
      targetId: "message-4",
      targetReady: true,
      messageCount: 6,
    })),
    {
      next: {
        targetId: "message-4",
        locatedAtMessageCount: 4,
        resumed: true,
      },
      suppress: false,
      forceResume: false,
    },
  );
});

test("stream location carries both conversation and message identity", () => {
  match(generationTileSource, /conversationId: item\.conversation_id/);
  match(generationTileSource, /scrollTo: item\.message_id/);
  match(generationTileSource, /router\.push\(`\/\?\$\{query\.toString\(\)\}`\)/);
});

test("mobile composer uses one visual viewport coordinate system", () => {
  match(viewportSource, /const viewportBottom = viewportTop \+ viewportHeight/);
  match(mobileComposerSource, /visualBottom - rect\.bottom/);
  match(mobileComposerSource, /type ComposerPanel =/);
  doesNotMatch(mobileComposerSource, /window\.innerHeight - rect\.bottom/);
  doesNotMatch(
    mobileComposerSource,
    /aspectSheetOpen|reasoningSheetOpen|advancedSheetOpen/,
  );
});

test("mobile top bar has one drawer entry and an explicit mode selector", () => {
  match(mobileTopBarSource, /aria-label="打开会话侧栏"/);
  match(mobileTopBarSource, /SegmentedControl<"chat" \| "image">/);
  doesNotMatch(mobileTopBarSource, /PanelLeft|mode === "image" \? "chat" : "image"/);
});

test("mobile tab bar height and reserved space share the responsive token", () => {
  match(
    mobileTabBarSource,
    /h-\[var\(--mobile-tabbar-h\)\] min-h-\[var\(--mobile-tabbar-h\)\]/,
  );
  match(
    mobileMeSource,
    /paddingBottom: "calc\(var\(--mobile-tabbar-height\) \+ 12px\)"/,
  );
  match(
    mobileStreamSource,
    /paddingBottom: "var\(--mobile-tabbar-height\)"/,
  );
  match(
    settingsShellSource,
    /max-md:mb-\[var\(--mobile-tabbar-height\)\]/,
  );
  match(
    mobileComposerSource,
    /"calc\(var\(--mobile-tabbar-height\) \+ 6px\)"/,
  );
  doesNotMatch(mobileTabBarSource, /\bh-14\b|\bmin-h-14\b/);
  doesNotMatch(mobileMeSource, /calc\(56px/);
  doesNotMatch(mobileStreamSource, /calc\(56px|calc\(72px/);
  doesNotMatch(settingsShellSource, /calc\(56px|calc\(112px/);
});

test("mobile navigation keeps current state and closes transient layers safely", () => {
  doesNotMatch(mobileTabBarSource, /router\.replace\(tab\.route\)/);
  match(mobileMeSource, /conversationId=\$\{encodeURIComponent\(conv\.id\)\}/);
  match(streamSearchSource, /inputRef\.current\?\.blur\(\)/);
  match(mobileDrawerSource, /isFetchNextPageError/);
  match(mobileDrawerSource, /setCurrentConv\(previousConvId\)/);
  match(desktopStudioSource, /onNavigate=\{closeSidebarDrawer\}/);
  match(sidebarSource, /const ARCHIVED_ROW_HEIGHT = 56/);
});

test("global focus and light text contracts remain accessible", () => {
  match(globalsSource, /outline: 2px solid var\(--focus-outline\) !important/);
  match(globalsSource, /--fg-2: #676E7A/);
  match(globalsSource, /--content-composer: 880px/);
  match(globalsSource, /--content-workbench: 1440px/);
  doesNotMatch(globalsSource, /body::before/);

  const studioBackground = cssBlock("  .lumen-studio-bg {");
  match(studioBackground, /var\(--bg-0\)/);
  doesNotMatch(studioBackground, /data:image|feTurbulence/);

  const darkTheme = cssBlock("  .dark {");
  const lightTheme = cssBlock("  .theme-light {");
  ok(
    contrastRatio(
      cssHex(darkTheme, "--fg-muted-aa"),
      cssHex(darkTheme, "--surface-overlay"),
    ) >= 4.5,
  );
  ok(
    contrastRatio(
      cssHex(lightTheme, "--fg-muted-aa"),
      cssHex(lightTheme, "--surface-overlay"),
    ) >= 4.5,
  );
});

test("shared fields merge caller descriptions with error and hint ids", () => {
  for (const fieldSource of [inputSource, textareaSource]) {
    match(fieldSource, /"aria-describedby": ariaDescribedBy/);
    match(
      fieldSource,
      /\[ariaDescribedBy, errorId, hintId\]\.filter\(Boolean\)\.join\(" "\)/,
    );
    match(fieldSource, /role="alert"/);
    match(fieldSource, /text-\[var\(--text-muted\)\]/);
  }
});
