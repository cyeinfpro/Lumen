import { doesNotMatch, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(new URL("./page.tsx", import.meta.url), "utf8");

test("video history remains reachable below xl layouts", () => {
  match(source, /md:overflow-y-auto/);
  match(source, /xl:overflow-hidden/);
  match(source, /xl:grid-cols-\[minmax\(0,1fr\)_minmax\(320px,380px\)\]/);
  match(source, /xl:sticky xl:top-4/);
});

test("video task list only becomes an internal scroller in xl side-panel layouts", () => {
  doesNotMatch(source, /max-h-\[720px\][^"]*overflow-hidden/);
  match(source, /xl:h-\[min\(720px,calc\(100dvh-5rem\)\)\] xl:overflow-hidden/);
  match(source, /xl:min-h-0 xl:flex-1 xl:overflow-y-auto xl:overscroll-contain/);
});

test("video prompt enhancement panel remains reachable above mobile chrome", () => {
  match(source, /promptEnhancePanelRef = useRef<HTMLDivElement \| null>\(null\)/);
  match(source, /promptEnhancePanelRef\.current\?\.scrollIntoView\(\{/);
  match(source, /block: "start"/);
  match(source, /pb-\[calc\(var\(--mobile-tabbar-height\)\+1rem\)\]/);
  match(source, /pb-\[calc\(var\(--mobile-tabbar-height\)\+2rem\)\]/);
  match(source, /scroll-mt-4 md:scroll-mt-6/);
});

test("video duration selector follows selected model action and resolution", () => {
  match(source, /durations_by_action_resolution\?\.\[action\]\?\.\[resolution\]/);
  match(source, /durations_by_action\?\.\[action\]/);
  match(source, /function durationOrPreferred\(current: number, options: number\[\]\)/);
  match(source, /setDurationS\(\(prev\) =>\s*durationOrPreferred\(prev, nextDurations\),\s*\)/);
});
