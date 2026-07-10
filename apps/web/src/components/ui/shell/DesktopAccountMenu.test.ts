import { deepEqual, doesNotMatch, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(
  new URL("./DesktopAccountMenu.tsx", import.meta.url),
  "utf8",
);
const popoverSource = readFileSync(
  new URL("../composer/desktop/DesktopPopover.tsx", import.meta.url),
  "utf8",
);
const { calculateDesktopPopoverPosition } = await import(
  new URL(
    "../composer/desktop/desktopPopoverPosition.ts",
    import.meta.url,
  ).href
);

test("desktop account menu does not expose Docker-only admin routes", () => {
  match(source, /if \(isAdmin && !desktop\) \{/);
});

test("desktop account popover stays within the viewport", () => {
  match(source, /align="right"/);
  match(popoverSource, /const panelWidth = panel\.offsetWidth/);
  match(popoverSource, /calculateDesktopPopoverPosition\(\{/);
  match(popoverSource, /maxWidth: "calc\(100vw - 24px\)"/);
  match(popoverSource, /useLayoutEffect\(\(\) =>/);
  match(popoverSource, /resizeObserver\?\.observe\(anchor\)/);
  doesNotMatch(popoverSource, /translateX/);
});

test("desktop popover positioning clamps every edge", () => {
  deepEqual(
    calculateDesktopPopoverPosition({
      anchorRect: {
        left: 930,
        right: 970,
        top: 700,
        bottom: 740,
        width: 40,
      },
      panelWidth: 256,
      panelHeight: 320,
      viewportWidth: 1000,
      viewportHeight: 800,
      align: "right",
    }),
    { left: 714, top: 372 },
  );

  deepEqual(
    calculateDesktopPopoverPosition({
      anchorRect: {
        left: 0,
        right: 32,
        top: 100,
        bottom: 132,
        width: 32,
      },
      panelWidth: 300,
      panelHeight: 80,
      viewportWidth: 800,
      viewportHeight: 600,
      align: "center",
    }),
    { left: 12, top: 12 },
  );

  deepEqual(
    calculateDesktopPopoverPosition({
      anchorRect: {
        left: 40,
        right: 80,
        top: -60,
        bottom: -20,
        width: 40,
      },
      panelWidth: 220,
      panelHeight: 120,
      viewportWidth: 800,
      viewportHeight: 600,
      align: "left",
    }),
    { left: 40, top: 12 },
  );
});
