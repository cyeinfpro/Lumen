// Lumen 前端统一 QueryClient 工厂。
// 每次调用返回新实例（SSR 友好 / 测试隔离）。
// 默认策略：
//  - staleTime 60s：SSR/client 共用窗口，避免水合后立即 refetch
//  - retry false：HTTP 层 apiFetch 已处理网络/短暂 5xx 重试，避免双层重试放大请求
//  - refetchOnWindowFocus false：Lumen 以 SSE 推送为主真相源，避免聚焦抖动
//  - mutation onError 兜底：防止计费操作静默失败导致用户重复点击（新-14 修复）
//
// 配合 QueryProvider 使用：在客户端组件 tree 顶层用 useState 把工厂调用固化一次。

import { QueryClient } from "@tanstack/react-query";

const DEFAULT_QUERY_STALE_TIME_MS = 60_000;

/** 由 UI 层注入的错误提示通道（lib 层不允许反向依赖 components）。 */
export type MutationErrorNotifier = (message: string) => void;

export function mutationErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "object" && error !== null && "message" in error) {
    const raw = (error as { message: unknown }).message;
    if (typeof raw === "string" && raw) return raw;
  }
  return "操作失败，重试";
}

function makeMutationErrorHandler(notify?: MutationErrorNotifier) {
  return (error: unknown) => {
    // 全局 mutation 错误兜底：60+ 个 mutation（含计费操作）缺少显式 onError。
    // 静默失败会让用户以为未执行而重复点击 → 重复扣费。
    // 优先级：显式 onError > 此兜底 > 静默吞噬。
    if (typeof window === "undefined") return;
    const message = mutationErrorMessage(error);
    if (notify) {
      notify(message);
      return;
    }
    // 未注入通道（测试 / SSR 预取）时至少留下痕迹，不要静默吞掉。
    console.error("[Mutation Error]", message, error);
  };
}

export function makeQueryClient(notify?: MutationErrorNotifier): QueryClient {
  // BUG-007: 防御性检查 — SSR 期间 Zustand store 可能未初始化。
  // 当前实现不依赖 store 状态，但保持工厂函数纯净以兼容 SSR 预取场景。
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: DEFAULT_QUERY_STALE_TIME_MS,
        retry: false,
        refetchOnWindowFocus: false,
      },
      mutations: {
        retry: 0,
        onError: makeMutationErrorHandler(notify),
      },
    },
  });
}
