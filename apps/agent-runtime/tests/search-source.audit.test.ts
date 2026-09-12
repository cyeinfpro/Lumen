import { afterEach, describe, expect, it, vi } from "vitest";

import { searchPublicWeb } from "../src/tools/web-search.js";

function urlOf(input: RequestInfo | URL): URL {
  return new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
}

function mockSources(duck: unknown, wiki: unknown): void {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = urlOf(input);
    if (url.hostname === "html.duckduckgo.com") return new Response("");
    return Response.json(url.hostname === "api.duckduckgo.com" ? duck : wiki);
  }));
}

afterEach(() => vi.unstubAllGlobals());

describe("search source integrity audit", () => {
  it("keeps the actual Wikipedia edition for a Latin title returned by Chinese search", async () => {
    mockSources({}, { query: { search: [{ title: "iPhone", snippet: "example" }] } });
    const result = await searchPublicWeb("手机", 5, undefined);
    expect(result.sources[0]?.url).toBe("https://zh.wikipedia.org/wiki/iPhone");
  });

  it("keeps the actual Wikipedia edition for a Chinese title returned by English search", async () => {
    mockSources({}, { query: { search: [{ title: "中文", snippet: "example" }] } });
    const result = await searchPublicWeb("language", 5, undefined);
    expect(result.sources[0]?.url).toBe(`https://en.wikipedia.org/wiki/${encodeURIComponent("中文")}`);
  });

  it("does not silently shorten a source URL into a different resource", async () => {
    mockSources({
      Heading: "long signed URL",
      AbstractURL: `https://example.test/document?signature=${"a".repeat(2100)}`,
      AbstractText: "answer",
    }, {});
    const result = await searchPublicWeb("query", 5, undefined);
    expect(result.sources).toEqual([]);
  });

  it("only shortens the display title, not the article identity", async () => {
    const title = "A".repeat(250);
    mockSources({}, { query: { search: [{ title, snippet: "example" }] } });
    const result = await searchPublicWeb("query", 5, undefined);
    expect(result.sources[0]?.title.length).toBeLessThanOrEqual(240);
    expect(result.sources[0]?.url).toBe(`https://en.wikipedia.org/wiki/${title}`);
  });

  it("preserves complete Unicode code points at display limits", async () => {
    const title = "a".repeat(239) + "🙂";
    mockSources({ Heading: title, AbstractURL: "https://example.test/article" }, {});
    const result = await searchPublicWeb("query", 5, undefined);
    expect(result.sources[0]?.title).toBe("a".repeat(239));
  });

  it("releases every rejected provider body instead of leaking unread streams", async () => {
    let cancellations = 0;
    vi.stubGlobal("fetch", vi.fn(async () => new Response(new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array([1])); },
      cancel() { cancellations += 1; },
    }), { status: 503 })));
    await expect(searchPublicWeb("query", 5, undefined)).rejects.toThrow(/providers unavailable/u);
    expect(cancellations).toBe(3);
  });
});
