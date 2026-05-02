"use client";

// Next.js 14+ global-error.tsx：捕获 root layout 级别的错误（app/error.tsx 无法覆盖）。
// 必须是 Client Component；接收 error + reset（或 unstable_retry）。
// BUG-021: root layout 崩溃时若没有此文件，Next.js 将显示空白页。

import { useEffect } from "react";
import Link from "next/link";
import { logError } from "@/lib/logger";

interface GlobalErrorPageProps {
  error: Error & { digest?: string };
  reset?: () => void;
  unstable_retry?: () => void;
}

export default function GlobalError({
  error,
  reset,
  unstable_retry,
}: GlobalErrorPageProps) {
  useEffect(() => {
    logError(error, {
      scope: "app/global-error",
      extra: { digest: error.digest },
    });
  }, [error]);

  const handleRetry = () => {
    if (typeof unstable_retry === "function") {
      unstable_retry();
      return;
    }
    if (typeof reset === "function") {
      reset();
      return;
    }
    if (typeof window !== "undefined") {
      try {
        window.location.reload();
      } catch {
        if (typeof window.alert === "function") {
          window.alert("当前环境不允许自动刷新，请手动刷新页面");
        }
      }
    }
  };

  const detail =
    (error.message || "未知错误") + (error.digest ? ` · ${error.digest}` : "");

  // 最小化 HTML/CSS（不依赖 primitives 组件，保证 root layout 场景下也能渲染）。
  return (
    <html lang="zh-CN">
      <body
        style={{
          margin: 0,
          padding: 0,
          minHeight: "100dvh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: "#0a0a0b",
          color: "#e4e4e7",
          fontFamily:
            'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
        }}
      >
        <div
          style={{
            maxWidth: 480,
            padding: "2rem",
            textAlign: "center",
          }}
        >
          <h1
            style={{
              fontSize: "1.25rem",
              fontWeight: 600,
              marginBottom: "0.75rem",
              color: "#fafafa",
            }}
          >
            页面出错
          </h1>
          <p
            style={{
              fontSize: "0.875rem",
              color: "#a1a1aa",
              marginBottom: "1.25rem",
              lineHeight: 1.6,
            }}
          >
            请刷新页面，或先返回首页。
          </p>
          {detail && (
            <pre
              style={{
                fontSize: "0.75rem",
                color: "#71717a",
                marginBottom: "1.25rem",
                padding: "0.75rem",
                borderRadius: 8,
                backgroundColor: "rgba(255,255,255,0.04)",
                wordBreak: "break-all",
                whiteSpace: "pre-wrap",
                textAlign: "left",
                fontFamily:
                  'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace',
              }}
            >
              {detail}
            </pre>
          )}
          <div style={{ display: "flex", gap: "0.75rem", justifyContent: "center", flexWrap: "wrap" }}>
            <button
              type="button"
              onClick={handleRetry}
              style={{
                padding: "0.5rem 1.25rem",
                borderRadius: 8,
                border: "1px solid rgba(242,169,58,0.3)",
                backgroundColor: "rgba(242,169,58,0.12)",
                color: "#f5a623",
                fontSize: "0.875rem",
                cursor: "pointer",
                fontWeight: 500,
              }}
            >
              重试
            </button>
            <Link
              href="/"
              style={{
                padding: "0.5rem 1.25rem",
                borderRadius: 8,
                border: "1px solid rgba(255,255,255,0.1)",
                backgroundColor: "rgba(255,255,255,0.05)",
                color: "#a1a1aa",
                fontSize: "0.875rem",
                textDecoration: "none",
                fontWeight: 500,
              }}
            >
              返回首页
            </Link>
          </div>
        </div>
      </body>
    </html>
  );
}
