"use client";

// 全局兜底错误边界。
// 在 layout 顶层包裹 children，捕获子树渲染错误，避免白屏。
// Next.js 16 的 app/error.tsx 只在 route 段触发；对于非 route 场景（例如某个 client-only
// 组件挂载阶段抛错），该 boundary 提供更早的兜底。
//
// 展示层统一走 ErrorState 原语，保证与 error.tsx / not-found.tsx 视觉一致。

import { Component, type ErrorInfo, type ReactNode } from "react";
import Link from "next/link";
import { ErrorState, Button } from "@/components/ui/primitives";
import { Home } from "lucide-react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    if (typeof console !== "undefined") {
      console.error("[ErrorBoundary] caught", error, info);
    }
  }

  private handleReload = (): void => {
    if (typeof window !== "undefined") {
      window.location.reload();
    }
  };

  private handleReset = (): void => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (!this.state.hasError) {
      return this.props.children;
    }
    if (this.props.fallback !== undefined) {
      return this.props.fallback;
    }
    const message =
      this.state.error instanceof Error ? this.state.error.message : "未知错误";
    return (
      <div className="safe-area flex min-h-[100dvh] w-full flex-1 items-center justify-center bg-[var(--bg-0)] px-4 py-6 sm:px-6">
        <div className="w-full min-w-0 max-w-md">
          <ErrorState
            title="页面出了点问题"
            description="渲染过程中发生了错误。可以尝试重试当前视图，或刷新整个页面。"
            detail={message}
            onRetry={this.handleReset}
            retryLabel="重试"
            secondaryAction={
              <div className="flex w-full min-w-0 flex-col gap-2 sm:w-auto sm:flex-row">
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={this.handleReload}
                  className="min-h-11 w-full sm:w-auto"
                >
                  刷新页面
                </Button>
                <Link
                  href="/"
                  className="inline-flex min-h-11 w-full items-center justify-center gap-1.5 rounded-[var(--radius-control)] px-3 text-sm font-medium text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:shadow-[var(--ring)] sm:w-auto"
                >
                  <Home className="h-3.5 w-3.5" aria-hidden />
                  返回首页
                </Link>
              </div>
            }
          />
        </div>
      </div>
    );
  }
}
