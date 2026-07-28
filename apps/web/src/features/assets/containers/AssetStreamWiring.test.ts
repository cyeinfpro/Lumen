import { match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

for (const file of ["DesktopAssetStream.tsx", "MobileAssetStream.tsx"]) {
  test(`${file} debounces search into the server feed filters`, () => {
    const source = readFileSync(new URL(`./${file}`, import.meta.url), "utf8");
    match(source, /const debouncedQ = useDebouncedStreamSearch\(q\)/);
    match(source, /\(\) => \(\{ \.\.\.queryFilters, q: debouncedQ \}\)/);
    match(source, /useStreamFeedQuery\(filters\)/);
    match(source, /useDeferredValue\(q\)/);
  });
}
