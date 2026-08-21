import { deepEqual, equal, match } from "node:assert/strict";
import { test } from "node:test";

import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";

const {
  buildStreamFeedQuery,
  normalizeStreamFeedFilters,
  normalizeStreamSearchQuery,
} = loadTsModule(
  new URL("./queries.ts", import.meta.url),
  {
    "@tanstack/react-query": { useInfiniteQuery: () => undefined },
    "react": { useEffect: () => undefined, useState: () => [null, () => undefined] },
    "@/lib/api/http": { apiFetch: () => undefined },
  },
) as {
  buildStreamFeedQuery(
    filters: Record<string, unknown>,
    limit: number,
    cursor?: string,
  ): string;
  normalizeStreamFeedFilters(
    filters: Record<string, unknown>,
  ): Record<string, unknown>;
  normalizeStreamSearchQuery(value?: string | null): string | null;
};

test("search query normalization removes blank cache fragments", () => {
  equal(normalizeStreamSearchQuery("  "), null);
  equal(normalizeStreamSearchQuery("  old   target  "), "old target");
  deepEqual(normalizeStreamFeedFilters({ q: "   " }), {
    ratio: null,
    has_ref: false,
    q: null,
  });
});

test("feed request and query key include normalized server q", () => {
  const query = buildStreamFeedQuery(
    {
      ratio: "1:1",
      has_ref: true,
      q: "  page   twenty ",
    },
    30,
    "cursor-1",
  );
  match(query, /limit=30/);
  match(query, /cursor=cursor-1/);
  match(query, /ratio=1%3A1/);
  match(query, /has_ref=1/);
  match(query, /q=page\+twenty/);
  equal(
    normalizeStreamFeedFilters({ q: "page twenty" }).q,
    "page twenty",
  );
});
