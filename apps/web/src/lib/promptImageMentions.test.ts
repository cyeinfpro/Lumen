import { deepEqual, equal } from "node:assert/strict";
import { test } from "node:test";

const {
  findInvalidImageMentionLabels,
  insertImageMentionToken,
  remapPromptImageMentions,
  serializePromptImageMentionsForRequest,
} = await import(new URL("./promptImageMentions.ts", import.meta.url).href);

const a = { id: "img-a" };
const b = { id: "img-b" };
const c = { id: "img-c" };

test("insertImageMentionToken adds readable spacing at the caret", () => {
  deepEqual(insertImageMentionToken("use", 2, 3, 3), {
    text: "use @图2",
    selectionStart: 7,
    selectionEnd: 7,
  });
  deepEqual(insertImageMentionToken("use  now", 1, 4, 4), {
    text: "use @图1 now",
    selectionStart: 7,
    selectionEnd: 7,
  });
});

test("remapPromptImageMentions follows attachment order changes", () => {
  equal(
    remapPromptImageMentions("先看 @图2，再看 @图3", [a, b, c], [b, c, a]),
    "先看 @图1，再看 @图2",
  );
});

test("remapPromptImageMentions marks removed references", () => {
  equal(
    remapPromptImageMentions("保留 @图1，删除 @图2", [a, b], [a]),
    "保留 @图1，删除 \u200b@已移除",
  );
});

test("serializePromptImageMentionsForRequest converts valid and internal removed references", () => {
  equal(
    serializePromptImageMentionsForRequest("参考 @图1、@图3 和 \u200b@已移除", [
      a,
      b,
    ]),
    "参考 [image 1]、[removed image] 和 [removed image]",
  );
});

test("serializePromptImageMentionsForRequest preserves literal removed text", () => {
  equal(
    serializePromptImageMentionsForRequest("文字里的 @已移除 不应被改写", [a]),
    "文字里的 @已移除 不应被改写",
  );
});

test("findInvalidImageMentionLabels reports out-of-range references once", () => {
  deepEqual(findInvalidImageMentionLabels("看 @图1、@图9、@图9", 2), ["@图9"]);
});
