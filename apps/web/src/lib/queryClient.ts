// Lumen 前端统一 QueryClient 工厂。
// 每次调用返回新实例（SSR 友好 / 测试隔离）。
// 默认策略：
//  - staleTime 30s：绝大多数只读视图都能吃到缓存，避免窗口切回/tab 抖动造成的无谓请求
//  - retry 1：网关/后端瞬时 5xx 只重试一次，不堆积
//  - refetchOnWindowFocus false：Lumen 以 SSE 推送为主真相源，避免聚焦抖动
//
// 配合 QueryProvider 使用：在客户端组件 tree 顶层用 useState 把工厂调用固化一次。

import { QueryClient } from "@tanstack/react-query";

export function makeQueryClient(): QueryClient {
  // BUG-007: 防御性检查 — SSR 期间 Zustand store 可能尚未初始化。
  // 当前实现不依赖 store 状态，但保持工厂函数纯净以兼容 SSR 预取场景。
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: typeof window === "undefined" ? 60_000 : 30_000,
        retry: 1,
        refetchOnWindowFocus: false,
      },
      mutations: {
        retry: 0,
      },
    },
  });
}
