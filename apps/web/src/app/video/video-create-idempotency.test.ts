import { test } from "node:test";
import {
  deepEqual,
  notStrictEqual,
  ok,
  strictEqual,
} from "node:assert/strict";

import {
  canonicalVideoCreatePayload,
  releaseVideoCreateIdempotencyKey,
  resolveVideoCreateIdempotencyKey,
} from "./video-create-idempotency.ts";

/** 每个测试独立的 key 序列,避免 node:test 顶层用例并发共享计数器。 */
function makeFreshKey(): () => string {
  let seq = 0;
  return () => {
    seq += 1;
    return `key-${seq}`;
  };
}

const BASE_BODY = {
  action: "text_to_video",
  model: "volcano",
  prompt: "一条狗在草地上奔跑",
  duration_s: 5,
  resolution: "1080p",
  aspect_ratio: "16:9",
  generate_audio: false,
  seed: 42,
  watermark: false,
};

test("相同 payload 的歧义失败重提沿用同一 key(服务端回放,避免二次预扣)", () => {
  const freshKey = makeFreshKey();
  const first = resolveVideoCreateIdempotencyKey(null, BASE_BODY, freshKey);
  strictEqual(first.key, "key-1");

  // 网络失败后用户原样重提:指纹一致 → 沿用 key-1
  const retry = resolveVideoCreateIdempotencyKey(first.pending, BASE_BODY, freshKey);
  strictEqual(retry.key, "key-1");
});

test("修改参数后的重提生成新 key(后端指纹不同,旧 key 会 409)", () => {
  const freshKey = makeFreshKey();
  const first = resolveVideoCreateIdempotencyKey(null, BASE_BODY, freshKey);
  strictEqual(first.key, "key-1");

  const modified = {
    ...BASE_BODY,
    prompt: "一只猫在沙发上睡觉",
  };
  const retry = resolveVideoCreateIdempotencyKey(first.pending, modified, freshKey);
  strictEqual(retry.key, "key-2");
  notStrictEqual(retry.key, first.key);
});

test("payload 键序无关:相同内容不同插入顺序产生相同指纹", () => {
  const reordered = {
    watermark: false,
    seed: 42,
    generate_audio: false,
    aspect_ratio: "16:9",
    resolution: "1080p",
    duration_s: 5,
    prompt: "一条狗在草地上奔跑",
    model: "volcano",
    action: "text_to_video",
  };
  strictEqual(
    canonicalVideoCreatePayload(BASE_BODY),
    canonicalVideoCreatePayload(reordered),
  );
});

test("提交确认成功后释放:下次提交为新操作,生成新 key", () => {
  const freshKey = makeFreshKey();
  const first = resolveVideoCreateIdempotencyKey(null, BASE_BODY, freshKey);
  const afterSuccess = releaseVideoCreateIdempotencyKey(first.pending, null);
  strictEqual(afterSuccess, null);

  const next = resolveVideoCreateIdempotencyKey(afterSuccess, BASE_BODY, freshKey);
  strictEqual(next.key, "key-2");
});

test("服务端明确拒绝(4xx/5xx)释放 key;网络/超时歧义失败保留", () => {
  const freshKey = makeFreshKey();
  const first = resolveVideoCreateIdempotencyKey(null, BASE_BODY, freshKey);

  const rejected = releaseVideoCreateIdempotencyKey(first.pending, {
    status: 409,
  });
  strictEqual(rejected, null);

  const second = resolveVideoCreateIdempotencyKey(null, BASE_BODY, freshKey);
  const kept = releaseVideoCreateIdempotencyKey(second.pending, {
    status: 0,
  });
  deepEqual(kept, second.pending);

  // 无状态码的异常(网络层 TypeError 等)同样保留
  const third = resolveVideoCreateIdempotencyKey(null, BASE_BODY, freshKey);
  const keptTypeError = releaseVideoCreateIdempotencyKey(
    third.pending,
    new TypeError("fetch failed"),
  );
  deepEqual(keptTypeError, third.pending);
});

test("并发/跨操作互不串扰:两个提交链交错决策各自持有独立 key", () => {
  const freshKey = makeFreshKey();
  // 操作 A 与操作 B 交错提交(不同 prompt),决策彼此独立
  const a = resolveVideoCreateIdempotencyKey(null, BASE_BODY, freshKey);
  const b = resolveVideoCreateIdempotencyKey(
    null,
    { ...BASE_BODY, prompt: "B" },
    freshKey,
  );
  notStrictEqual(a.key, b.key);

  // A 失败重提(同参数)→ 沿用 A 的 key;B 不受影响
  const aRetry = resolveVideoCreateIdempotencyKey(a.pending, BASE_BODY, freshKey);
  strictEqual(aRetry.key, a.key);

  // B 成功释放后,新的 B 提交得到全新 key,不与 A 共享
  const bDone = releaseVideoCreateIdempotencyKey(b.pending, null);
  const bNext = resolveVideoCreateIdempotencyKey(
    bDone,
    { ...BASE_BODY, prompt: "B" },
    freshKey,
  );
  ok(bNext.key !== a.key);
  ok(bNext.key !== b.key);
});
