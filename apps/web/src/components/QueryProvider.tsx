"use client";

// 把 @tanstack/react-query 的 QueryClientProvider 贴在 App 根部的一层。
// 用 useState(() => makeQueryClient()) 保持单例——渲染期不要 new，否则每次 rerender 都清缓存。
// 仅在开发环境挂载 ReactQueryDevtools（生产 tree-shake 掉 panel 代码体积）。

import { useState } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { makeQueryClient } from "@/lib/queryClient";

export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => makeQueryClient());
  return (
    <QueryClientProvider client={client}>
      {children}
      {process.env.NODE_ENV !== "production" ? (
        <ReactQueryDevtools initialIsOpen={false} buttonPosition="bottom-left" />
      ) : null}
    </QueryClientProvider>
  );
}
