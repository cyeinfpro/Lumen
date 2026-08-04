// makeQueryClient 的 mutation onError 全局兜底测试（审计 I-1 / 新-14）。
// 兜底的价值在于「没有显式 onError 的 mutation 失败时也必须留下痕迹」，
// 同时不能抢走显式 onError 的位置（否则会出现双重提示，或泛化文案盖掉具体动作名）。

import { equal, ok } from "node:assert/strict";
import { test } from "node:test";
import * as reactQuery from "@tanstack/react-query";
import { MutationObserver, type QueryClient } from "@tanstack/react-query";

import { loadTsModule } from "../../test-support/load-ts-module.mjs";

const { makeQueryClient, mutationErrorMessage } = loadTsModule(
  new URL("./queryClient.ts", import.meta.url),
  { "@tanstack/react-query": reactQuery },
) as {
  makeQueryClient(notify?: (message: string) => void): QueryClient;
  mutationErrorMessage(error: unknown): string;
};

type Globals = { window?: unknown };

/**
 * 在指定的 window 存在性下跑异步回调。
 * 兜底对 SSR 预取显式短路（typeof window === "undefined" 直接 return），
 * Node 默认没有 window，所以浏览器路径必须先补上；且必须 await 完再还原，
 * 否则 onError 是在还原之后才被调用，测的就不是目标分支了。
 */
async function withWindow<T>(present: boolean, run: () => Promise<T>): Promise<T> {
  const globals = globalThis as Globals;
  const had = "window" in globals;
  const previous = globals.window;
  if (present) globals.window = previous ?? {};
  else if (had) delete globals.window;
  try {
    return await run();
  } finally {
    if (had) globals.window = previous;
    else delete globals.window;
  }
}

/** 捕获 console.error，验证未注入通道时不会静默吞掉错误。 */
async function captureConsoleError(run: () => Promise<void>): Promise<unknown[][]> {
  const calls: unknown[][] = [];
  const original = console.error;
  console.error = (...args: unknown[]) => {
    calls.push(args);
  };
  try {
    await run();
  } finally {
    console.error = original;
  }
  return calls;
}

async function runFailingMutation(
  client: QueryClient,
  message: string,
  options: Record<string, unknown> = {},
): Promise<void> {
  const observer = new MutationObserver(client, {
    mutationFn: async () => {
      throw new Error(message);
    },
    ...options,
  });
  await observer.mutate().catch(() => undefined);
}

test("mutation 错误文案归一化：Error / 类 Error 对象 / 兜底", () => {
  equal(mutationErrorMessage(new Error("余额不足")), "余额不足");
  equal(mutationErrorMessage({ message: "上游超时" }), "上游超时");
  // 空 message、非对象、null 都要落到兜底文案，不能返回空串让 toast 显示空白。
  equal(mutationErrorMessage(new Error("")), "操作失败，重试");
  equal(mutationErrorMessage({ message: 42 }), "操作失败，重试");
  equal(mutationErrorMessage("boom"), "操作失败，重试");
  equal(mutationErrorMessage(null), "操作失败，重试");
});

test("默认项：查询与 mutation 都不做双层重试，且挂上 onError 兜底", () => {
  const defaults = makeQueryClient().getDefaultOptions();
  equal(defaults.queries?.staleTime, 60_000);
  equal(defaults.queries?.retry, false);
  equal(defaults.queries?.refetchOnWindowFocus, false);
  // retry 必须为 0：计费操作静默重试会放大扣费。
  equal(defaults.mutations?.retry, 0);
  equal(typeof defaults.mutations?.onError, "function");
});

test("工厂每次返回独立实例，避免 SSR 跨请求串数据", () => {
  ok(makeQueryClient() !== makeQueryClient());
});

test("缺少显式 onError 的 mutation 失败时会走全局兜底提示", async () => {
  const messages: string[] = [];
  const client = makeQueryClient((message) => messages.push(message));
  await withWindow(true, () => runFailingMutation(client, "上游超时"));
  equal(messages.length, 1);
  equal(messages[0], "上游超时");
});

test("显式 onError 覆盖全局兜底，不会出现双重提示", async () => {
  const messages: string[] = [];
  const explicit: string[] = [];
  const client = makeQueryClient((message) => messages.push(message));
  await withWindow(true, () =>
    runFailingMutation(client, "上游超时", {
      onError: (error: Error) => explicit.push(`生成关键帧失败：${error.message}`),
    }),
  );
  // React Query 对 defaultOptions.mutations 做浅合并：显式 onError 直接替换兜底。
  // 这正是 StoryboardPages 这类页面能给出具体动作名的前提。
  equal(explicit.length, 1);
  equal(explicit[0], "生成关键帧失败：上游超时");
  equal(messages.length, 0);
});

test("未注入提示通道时兜底改走 console.error，不静默吞掉", async () => {
  const client = makeQueryClient();
  const calls = await captureConsoleError(() =>
    withWindow(true, () => runFailingMutation(client, "上游超时")),
  );
  equal(calls.length, 1);
  equal(calls[0][0], "[Mutation Error]");
  equal(calls[0][1], "上游超时");
});

test("SSR（无 window）不触发提示通道", async () => {
  const messages: string[] = [];
  const client = makeQueryClient((message) => messages.push(message));
  const calls = await captureConsoleError(() =>
    withWindow(false, () => runFailingMutation(client, "上游超时")),
  );
  equal(messages.length, 0);
  equal(calls.length, 0);
});
