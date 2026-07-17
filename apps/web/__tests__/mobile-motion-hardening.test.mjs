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

test("canceled swipe gestures never commit destructive actions", () => {
  const swipeRow = source(
    "src/components/ui/primitives/mobile/SwipeRow.tsx",
  );

  match(swipeRow, /event\.type === "pointercancel"/);
  match(swipeRow, /event\.type === "touchcancel"/);
  match(swipeRow, /resetTo\(0\);[\s\S]*return;/);
  match(swipeRow, /const visibleActions = actions\.slice\(0, 3\)/);
});

test("pull to refresh tracks one touch and cleans up every refresh generation", () => {
  const pullToRefresh = source(
    "src/components/ui/primitives/mobile/PullToRefresh.tsx",
  );

  match(pullToRefresh, /const activeTouchId = useRef<number \| null>\(null\)/);
  match(pullToRefresh, /trackedTouch\(e\.touches, activeTouchId\.current\)/);
  match(
    pullToRefresh,
    /Promise\.resolve\(\)\s*\.then\(\(\) => currentOnRefresh\(\)\)/,
  );
  match(pullToRefresh, /refreshGenerationRef\.current !== generation/);
  match(pullToRefresh, /window\.clearTimeout\(sweepTimerRef\.current\)/);
});

test("bottom sheets use direct manipulation, animated snap compensation, and an accessible close action", () => {
  const bottomSheet = source(
    "src/components/ui/primitives/mobile/BottomSheet.tsx",
  );

  match(bottomSheet, /const sheetY = useMotionValue\(0\)/);
  match(bottomSheet, /const settleToSnap = useCallback/);
  match(bottomSheet, /y: sheetY/);
  match(bottomSheet, /aria-label="关闭面板"/);
  match(bottomSheet, /aria-orientation="vertical"/);
  doesNotMatch(
    bottomSheet,
    /dragConstraints=\{\{\s*top:\s*0,\s*bottom:\s*0\s*\}\}/,
  );
});

test("pressable honors reduced motion and disabled anchor semantics", () => {
  const pressable = source(
    "src/components/ui/primitives/mobile/Pressable.tsx",
  );

  match(pressable, /const reduceMotion = useReducedMotion\(\)/);
  match(pressable, /scale: reduceMotion \? 1 : PRESS_SCALE\[pressScale\]/);
  match(pressable, /tabIndex=\{disabled \? -1 : tabIndex\}/);
  match(pressable, /if \(disabled\) \{\s*event\.preventDefault\(\);/);
});

test("lightbox and composer gestures avoid stale direction and layout animation", () => {
  const lightbox = source(
    "src/components/ui/lightbox/LightboxGestures.ts",
  );
  const desktopComposer = source(
    "src/components/ui/composer/desktop/DesktopComposerPill.tsx",
  );
  const mobileComposer = source(
    "src/components/ui/composer/mobile/MobileComposerPill.tsx",
  );

  match(lightbox, /dx \+ projectMomentum\(vxRef\.current\)/);
  match(lightbox, /dy \+ projectMomentum\(vyRef\.current\)/);
  for (const composer of [desktopComposer, mobileComposer]) {
    match(composer, /<Pressable/);
    doesNotMatch(composer, /height:\s*"auto"/);
    doesNotMatch(composer, /whileTap=/);
  }
});
