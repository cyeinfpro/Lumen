import { doesNotMatch, match, ok } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

function source(file: string): string {
  return readFileSync(new URL(`./${file}`, import.meta.url), "utf8");
}

for (const file of ["DesktopAssetStream.tsx", "MobileAssetStream.tsx"]) {
  test(`${file} debounces search into the server feed filters`, () => {
    const contents = source(file);
    match(contents, /const debouncedQ = useDebouncedStreamSearch\(q\)/);
    match(contents, /\(\) => \(\{ \.\.\.queryFilters, q: debouncedQ \}\)/);
    match(contents, /useStreamFeedQuery\(filters\)/);
    match(contents, /useDeferredValue\(q\)/);
  });
}

for (const [file, feedComponent] of [
  ["DesktopAssetStream.tsx", "StreamFeedState"],
  ["MobileAssetStream.tsx", "MobileStreamFeedState"],
] as const) {
  test(`${file} keeps asset controls together directly before the masonry`, () => {
    const contents = source(file);
    const toolbarStart = contents.indexOf("<StreamOverview");
    const masonryStart = contents.indexOf(`<${feedComponent}`);

    ok(toolbarStart > 0);
    ok(masonryStart > toolbarStart);
    const toolbar = contents.slice(toolbarStart, masonryStart);
    match(toolbar, /onToggleSearch=\{onToggleSearch\}/);
    match(toolbar, /onToggleFilter=\{onToggleFilter\}/);
    match(toolbar, /onRefresh=/);
    match(toolbar, /onToggleSelectionMode=\{toggleSelectionMode\}/);
    match(toolbar, /<StreamSearchBar/);
    match(toolbar, /<FilterBar/);
  });
}

test("desktop global navigation receives no asset-private toolbar", () => {
  const contents = source("DesktopAssetStream.tsx");
  match(contents, /<DesktopTopNav active="assets" \/>/);
  doesNotMatch(contents, /<DesktopTopNav[\s\S]{0,300}\bright=/);
  doesNotMatch(contents, /function StreamToolbar/);
});

test("mobile asset deletion uses the same authorized mutation as desktop", () => {
  const contents = source("MobileAssetStream.tsx");
  match(contents, /useDeleteStreamImageMutation\(\)/);
  match(contents, /onDeleteImage=\{deleteStreamImage\}/);
});

test("mobile asset header has no private feed controls", () => {
  const contents = source("MobileAssetStream.tsx");
  match(contents, /<StreamTopBar compact=\{compact\} \/>/);
  const topBar = readFileSync(
    new URL("../ui/StreamTopBar.tsx", import.meta.url),
    "utf8",
  );
  doesNotMatch(topBar, /onToggleSearch|onToggleFilter|countLabel/);
});

test("asset toolbar delegates its primary create action to Button", () => {
  const overview = readFileSync(
    new URL("../ui/StreamOverview.tsx", import.meta.url),
    "utf8",
  );
  match(overview, /import \{ Button, IconButton \} from "@\/components\/ui\/primitives"/);
  match(overview, /<Button\s+variant="primary"[\s\S]*?>\s*创作\s*<\/Button>/);
  doesNotMatch(overview, /<button[\s\S]{0,240}bg-\[var\(--accent\)\][\s\S]{0,120}创作/);
});
