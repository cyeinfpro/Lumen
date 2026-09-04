import { deepEqual, doesNotMatch, equal, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { loadTsModule } from "../../../../../test-support/load-ts-module.mjs";

const {
  collectModelLibraryStyleTags,
  filterModelLibraryItemsByStyleTag,
} = loadTsModule(
  new URL("./modelLibraryStyleFilters.ts", import.meta.url),
) as {
  collectModelLibraryStyleTags(
    items: Array<{ style_tags: string[] }>,
    limit?: number,
  ): string[];
  filterModelLibraryItemsByStyleTag<T extends { style_tags: string[] }>(
    items: T[],
    styleTag: string,
  ): T[];
};

const { createModelLibrarySelectionAction } = loadTsModule(
  new URL("./modelLibrarySelection.ts", import.meta.url),
) as {
  createModelLibrarySelectionAction<T extends { id: string }>(
    items: T[],
    label: string,
    onSelectItem: (item: T) => void,
    pending?: boolean,
  ): {
    label: string;
    pending: boolean;
    onClick: (item: { id: string }) => void;
  };
};

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const browserSource = source("./ModelLibraryBrowser.tsx");
const viewSource = source("./ModelLibraryBrowserView.tsx");
const cardSource = source("./ModelLibraryCard.tsx");
const pageSource = source("./ModelLibraryPage.tsx");
const dialogSource = source("../components/ModelLibraryDialog.tsx");

const models = [
  { id: "a", style_tags: ["清冷高级", "极简中性", "清冷高级"] },
  { id: "b", style_tags: ["知性通勤", "清冷高级"] },
  { id: "c", style_tags: ["运动阳光"] },
];

test("style direction options come from real tags and rank by model coverage", () => {
  deepEqual(collectModelLibraryStyleTags(models), [
    "清冷高级",
    "极简中性",
    "知性通勤",
    "运动阳光",
  ]);
  deepEqual(collectModelLibraryStyleTags(models, 2), ["清冷高级", "极简中性"]);
});

test("style direction filtering is exact, normalized, and independently clearable", () => {
  deepEqual(
    filterModelLibraryItemsByStyleTag(models, "  清冷高级 ").map((item) => item.id),
    ["a", "b"],
  );
  deepEqual(
    filterModelLibraryItemsByStyleTag(models, "通勤").map((item) => item.id),
    [],
  );
  equal(filterModelLibraryItemsByStyleTag(models, "").length, models.length);
});

test("browser exposes age, appearance, and real tag direction as separate filters", () => {
  match(viewSource, /ChipRowGroup label="年龄段"/);
  match(viewSource, /ChipRowGroup label="外貌方向"/);
  match(viewSource, /ChipRowGroup label="气质方向"/);
  match(browserSource, /filterModelLibraryItemsByStyleTag\(unfilteredItems, styleTag\)/);
  match(browserSource, /if \(styleTag\) count \+= 1/);
});

test("desktop and mobile filters expose their selected state semantically", () => {
  match(viewSource, /role="radiogroup" aria-label="模特来源"/);
  match(viewSource, /role="radio"/);
  match(viewSource, /aria-checked=\{active\}/);
  match(viewSource, /role="group"[\s\S]*aria-label=\{label\}/);
  match(viewSource, /aria-pressed=\{active\}/);
  const overlays = source("./ModelLibraryBrowserDialogs.tsx");
  for (const label of ["年龄段", "外貌方向", "气质方向", "来源"]) {
    match(overlays, new RegExp(`aria-label="${label}"`));
  }
});

test("dialog cards and lightbox use the live candidate-selection callback", () => {
  const selected: string[] = [];
  const action = createModelLibrarySelectionAction(
    [{ id: "model-a" }, { id: "model-b" }],
    "选入候选",
    (item) => selected.push(item.id),
  );

  equal(action.label, "选入候选");
  equal(action.pending, false);
  action.onClick({ id: "model-b" });
  action.onClick({ id: "missing" });
  deepEqual(selected, ["model-b"]);

  const pendingAction = createModelLibrarySelectionAction(
    [{ id: "model-a" }],
    "选入候选",
    () => undefined,
    true,
  );
  equal(pendingAction.pending, true);
  match(browserSource, /selectActionLabel = "选入候选"/);
  match(
    browserSource,
    /if \(mode !== "dialog" \|\| !onSelectItem \|\| isLoserView\) return null/,
  );
  match(browserSource, /createModelLibrarySelectionAction\([\s\S]*selectActionPending/);
  match(viewSource, /props\.mode === "dialog"[\s\S]*\? props\.onSelectItem/);
  match(viewSource, /selecting=\{props\.selectActionPending\}/);
  match(cardSource, /loading=\{selecting\}/);
  match(cardSource, /onClick=\{\(\) => onSelect\(item\)\}/);
  match(cardSource, /selecting \? "选择中" : selectLabel \?\? "选入候选"/);
  match(dialogSource, /if \(selectionIdentityRef\.current\) return/);
  equal(
    (dialogSource.match(/selectActionPending=\{selectItem\.isPending\}/g) ?? [])
      .length,
    2,
  );
});

test("model cards render the API-provided usage count without client inference", () => {
  match(cardSource, /item\.usage_count/);
  match(cardSource, /已匹配 \{item\.usage_count\} 套/);
  match(cardSource, /aria-label=\{`已匹配生成 \$\{item\.usage_count\} 套`\}/);
  doesNotMatch(cardSource, /usage_count\s*\?\?|Math\.(?:max|round).*usage/);
  match(browserSource, /usage_count: 0/);
});

test("upload and mobile filter overlays use focus-managed shared primitives", () => {
  const overlays = source("./ModelLibraryBrowserDialogs.tsx");
  match(overlays, /<Dialog[\s\S]*initialFocusRef=\{nameInputRef\}/);
  match(overlays, /<BottomSheet[\s\S]*ariaLabel="筛选"/);
  doesNotMatch(overlays, /aria-modal="true"|document\.addEventListener\("keydown"/);
});

test("mobile library hides the desktop header without relying on page-header precedence", () => {
  match(
    pageSource,
    /<div className="hidden md:block">\s*<LibraryHeader/,
  );
  doesNotMatch(pageSource, /className="page-header hidden md:grid"/);
});

test("card billing badges are based only on returned metadata", () => {
  const freeHelper = cardSource.slice(
    cardSource.indexOf("function modelLibraryItemIsFree"),
    cardSource.indexOf("function modelLibraryAppearanceLabel"),
  );
  doesNotMatch(freeHelper, /isLoser/);
  match(freeHelper, /item\.billing_free === true/);
  match(freeHelper, /item\.billing_label === "free"/);
});
