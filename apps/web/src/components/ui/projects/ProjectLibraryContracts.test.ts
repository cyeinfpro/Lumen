import { doesNotMatch, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string) {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const uploadDialogSource = source("./library/ModelLibraryBrowserDialogs.tsx");
const modelLibraryDialogSource = source("./components/ModelLibraryDialog.tsx");
const constraintPanelSource = source("./components/ConstraintPanel.tsx");
const posterConstraintPanelSource = source(
  "./components/PosterConstraintPanel.tsx",
);

test("model library uploads keep visible category choices authoritative", () => {
  match(uploadDialogSource, /age_segment:\s*form\.age_segment/);
  match(uploadDialogSource, /gender:\s*form\.gender/);
  match(
    uploadDialogSource,
    /const appearanceDirection =\s*form\.appearance_direction \|\|/,
  );
  doesNotMatch(uploadDialogSource, /const ageSegment =\s*embedded/);
  doesNotMatch(uploadDialogSource, /const gender =\s*embedded/);
});

test("desktop project dialogs lock background scrolling through the shared hook", () => {
  match(
    modelLibraryDialogSource,
    /useBodyScrollLock\(isMobile === false && open\)/,
  );
  match(
    constraintPanelSource,
    /useBodyScrollLock\(isDesktop && open\)/,
  );
});

test("poster constraints use the shared modal focus and scroll lifecycle", () => {
  match(posterConstraintPanelSource, /useModalLayer\(\{/);
  match(
    posterConstraintPanelSource,
    /open:\s*isDesktop && open,\s*rootRef:\s*drawerRef,\s*onClose,/,
  );
  match(
    posterConstraintPanelSource,
    /useBodyScrollLock\(isDesktop && open\)/,
  );
  match(posterConstraintPanelSource, /ref=\{drawerRef\}/);
  match(posterConstraintPanelSource, /aria-labelledby=\{titleId\}/);
  match(posterConstraintPanelSource, /tabIndex=\{-1\}/);
  match(posterConstraintPanelSource, /onKeyDown=\{onDrawerKeyDown\}/);
  match(
    posterConstraintPanelSource,
    /mobile-dialog-scroll min-h-0 min-w-0 flex-1 overflow-y-auto/,
  );
});
