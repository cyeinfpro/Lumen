import { afterEach, describe, expect, it, vi } from "vitest";

import { searchPublicWeb } from "../src/tools/web-search.js";

describe("bounded public web search", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("merges fixed public providers and removes markup from snippets", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
      if (url.includes("html.duckduckgo")) {
        return new Response(`
          <div class="result">
            <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com%2Fguide">Guide</a>
            <a class="result__snippet">Current <b>documentation</b></a>
          </div>
        `);
      }
      if (url.includes("api.duckduckgo")) {
        return new Response(JSON.stringify({
          Heading: "Lumen",
          AbstractText: "A bounded answer",
          AbstractURL: "https://example.com/lumen#section",
          RelatedTopics: [],
        }));
      }
      return new Response(JSON.stringify({
        query: {
          search: [{ title: "Lumen docs", snippet: "<span>Official</span> docs" }],
        },
      }));
    }));

    const result = await searchPublicWeb("Lumen", 5, undefined);

    expect(result.answer).toBe("A bounded answer");
    expect(result.sources).toEqual(expect.arrayContaining([
      expect.objectContaining({
        title: "Lumen",
        url: "https://example.com/lumen",
      }),
      expect.objectContaining({
        title: "Guide",
        url: "https://docs.example.com/guide",
        snippet: "Current documentation",
      }),
      expect.objectContaining({
        title: "Lumen docs",
        snippet: "Official docs",
      }),
    ]));
  });

  it("fails when every fixed provider is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new Error("offline");
    }));

    await expect(searchPublicWeb("query", 5, undefined)).rejects.toThrow(
      /providers unavailable/u,
    );
  });
});
