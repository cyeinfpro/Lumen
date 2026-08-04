"use client";

// React 19 兼容的 ErrorBoundary。Stage 内部抛出（例如 step.output_json 异常结构）时
// 不要让整个详情页崩，给一个 editorial 安全降级面板，并提供"重试"按钮。

import { AlertTriangle, RefreshCw } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  resetKeys?: ReadonlyArray<unknown>;
}

interface State {
  error: Error | null;
}

export class StageErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidUpdate(prevProps: Props) {
    if (this.state.error && this.props.resetKeys) {
      const changed =
        !prevProps.resetKeys ||
        prevProps.resetKeys.length !== this.props.resetKeys.length ||
        prevProps.resetKeys.some((value, index) => value !== this.props.resetKeys?.[index]);
      if (changed) this.setState({ error: null });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    if (typeof window !== "undefined") {
      console.error("[ProjectStage] crashed", error, info.componentStack);
    }
  }

  reset = () => this.setState({ error: null });

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <section className="border-y border-danger-border bg-danger-soft px-4 py-8">
        <div className="flex items-start gap-4">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-danger-border text-[var(--danger)]">
            <AlertTriangle className="h-4 w-4" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="type-caption text-[var(--danger)]">
              错误
            </p>
            <h3 className="type-section-title mt-1">
              阶段渲染异常
            </h3>
            <p className="mt-1 break-words type-caption text-[var(--fg-1)]">
              {this.state.error.message || "未知错误"}
            </p>
            <button
              type="button"
              onClick={this.reset}
              className="mt-4 inline-flex min-h-10 items-center gap-2 rounded-full border border-[var(--border)] px-4 type-caption text-[var(--fg-0)] transition-colors hover:border-accent-border hover:text-accent"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              重试本阶段
            </button>
          </div>
        </div>
      </section>
    );
  }
}
