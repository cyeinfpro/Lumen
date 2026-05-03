"use client";

// React 19 兼容的 ErrorBoundary。Stage 内部抛出（例如 step.output_json 异常结构）时
// 不要让整个详情页崩，给一个安全降级面板，并提供"重新加载"按钮。

import { AlertTriangle, RefreshCw } from "lucide-react";
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** 触发重置的 keys 变化时清掉错误（例如 workflowId / current_step 切换） */
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
      <section className="rounded-md border border-[var(--danger)]/30 bg-[var(--danger-soft)] p-5 text-sm">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 inline-flex h-8 w-8 items-center justify-center rounded-md bg-[var(--danger)]/20 text-[var(--danger)]">
            <AlertTriangle className="h-4 w-4" />
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="text-base font-medium text-[var(--fg-0)]">阶段渲染异常</h3>
            <p className="mt-1 break-words text-xs text-[var(--fg-1)]">
              {this.state.error.message || "未知错误"}
            </p>
            <button
              type="button"
              onClick={this.reset}
              className="mt-3 inline-flex h-9 items-center gap-1.5 rounded-md border border-[var(--border)] bg-white/[0.04] px-3 text-xs text-[var(--fg-0)] transition-colors hover:bg-white/[0.08]"
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
