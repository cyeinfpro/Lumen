import { deepEqual, equal, match } from "node:assert/strict";
import { test } from "node:test";

import { loadTsModule } from "../../../../test-support/load-ts-module.mjs";

const apiCalls: Array<{ path: string; init: Record<string, unknown> }> = [];

const {
  buildStreamFeedQuery,
  deleteStreamImage,
  normalizeStreamFeedFilters,
  normalizeStreamSearchQuery,
} = loadTsModule(
  new URL("./queries.ts", import.meta.url),
  {
    "@tanstack/react-query": {
      useInfiniteQuery: () => undefined,
      useMutation: () => undefined,
      useQueryClient: () => undefined,
    },
    "react": { useEffect: () => undefined, useState: () => [null, () => undefined] },
    "@/lib/api/http": {
      apiFetch: (path: string, init: Record<string, unknown>) => {
        apiCalls.push({ path, init });
        return Promise.resolve({ ok: true });
      },
    },
  },
) as {
  buildStreamFeedQuery(
    filters: Record<string, unknown>,
    limit: number,
    cursor?: string,
  ): string;
  deleteStreamImage(imageId: string): Promise<{ ok: boolean }>;
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

test("stream image deletion uses the authorized image route", async () => {
  apiCalls.length = 0;
  const result = await deleteStreamImage("image / one");

  deepEqual(result, { ok: true });
  deepEqual(apiCalls, [
    {
      path: "/images/image%20%2F%20one",
      init: { method: "DELETE" },
    },
  ]);
});
