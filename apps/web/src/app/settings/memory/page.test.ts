import { doesNotMatch, match, ok } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

const pageSource = source("./page.tsx");
const modelSource = source("./useMemoryPageModel.ts");
const queriesSource = source("./queries/useMemoryQueries.ts");
const mutationsSource = source("./mutations/useMemoryMutations.ts");

test("memory page stays below the page budget and only composes modules", () => {
  const lineCount = pageSource.split("\n").length;
  ok(lineCount < 800, `page.tsx must stay below 800 lines, got ${lineCount}`);
  match(pageSource, /useMemoryPageModel\(\)/);
  match(pageSource, /<MemoryScopeSidebar \{\.\.\.model\.scopeSidebar\} \/>/);
  match(pageSource, /<MemoryLibrarySection \{\.\.\.model\.memoryLibrary\} \/>/);
  match(pageSource, /<MemoryCapabilityModal \{\.\.\.model\.capabilityModal\} \/>/);
  doesNotMatch(
    pageSource,
    /\b(?:useQuery|useMutation|useState|useMemo)\s*\(/,
  );
  doesNotMatch(pageSource, /@\/lib\/apiClient/);
});

test("memory queries keep every private query user scoped and identity gated", () => {
  match(queriesSource, /userMemoryQueryKeys\.settings\(userScope\.userId\)/);
  match(queriesSource, /userMemoryQueryKeys\.scopes\(userScope\.userId\)/);
  match(
    queriesSource,
    /userMemoryQueryKeys\.items\(userScope\.userId, selectedScope\)/,
  );
  match(queriesSource, /userMemoryQueryKeys\.staging\(userScope\.userId\)/);
  match(queriesSource, /userMemoryQueryKeys\.timeline\(userScope\.userId\)/);
  ok(
    (queriesSource.match(/enabled: userScope\.enabled/g) ?? []).length === 5,
    "all five memory queries must remain identity gated",
  );
  doesNotMatch(queriesSource, /queryKey:\s*\["me",\s*"memory"/);
});

test("memory mutations invalidate only the current user's memory root", () => {
  match(
    mutationsSource,
    /queryKey: userMemoryQueryKeys\.all\(userScope\.userId\)/,
  );
  match(mutationsSource, /if \(!userScope\.enabled\) return/);
  doesNotMatch(mutationsSource, /queryKey:\s*\["me",\s*"memory"/);
});

test("memory model owns page state and coordinates queries with mutations", () => {
  match(modelSource, /const userScope = useUserQueryScope\(\)/);
  match(
    modelSource,
    /const queries = useMemoryQueries\(userScope, selectedScope\)/,
  );
  match(modelSource, /const mutations = useMemoryMutations\(\{/);
  doesNotMatch(modelSource, /\buseQuery\(|\buseMutation\(/);
});
