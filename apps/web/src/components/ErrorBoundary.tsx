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
      <div className="min-h-[100dvh] w-full flex-1 flex items-center justify-center bg-[var(--bg-0)] px-6">
        <div className="max-w-md w-full">
          <ErrorState
            title="页面出了点问题"
            description="渲染过程中发生了错误。可以尝试重试当前视图，或刷新整个页面。"
            detail={message}
            onRetry={this.handleReset}
            retryLabel="重试"
            secondaryAction={
              <>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={this.handleReload}
                >
                  刷新页面
                </Button>
                <Link href="/">
                  <Button
                    variant="ghost"
                    size="sm"
                    leftIcon={<Home className="w-3.5 h-3.5" />}
                  >
                    返回首页
                  </Button>
                </Link>
              </>
            }
          />
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;
