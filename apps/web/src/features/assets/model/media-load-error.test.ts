import { equal, match } from "node:assert/strict";
import { test } from "node:test";

const { mediaLoadError } = await import(new URL("./mediaLoadError.ts", import.meta.url).href);

test("media asset failures distinguish permissions from network and server errors using HTTP status", () => {
  const forbidden = mediaLoadError({ status: 403, message: "media failure" });
  equal(forbidden.title, "访问受限");
  equal(forbidden.retryable, false);
  equal(mediaLoadError({ status: 401 }).title, "登录已失效");
  equal(mediaLoadError({ status: 0 }).title, "网络连接异常");
  equal(mediaLoadError(new TypeError("Failed to fetch")).title, "网络连接异常");
  equal(mediaLoadError({ status: 503 }).title, "记录加载失败");
  match(mediaLoadError({ status: 503 }).detail, /已加载内容仍保留/);
});
