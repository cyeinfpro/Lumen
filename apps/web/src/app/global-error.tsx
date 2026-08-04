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
        // Root layout 已崩溃时不能依赖 toast；保持当前错误界面即可。
      }
    }
  };

  const detail =
    (error.message || "未知错误") + (error.digest ? ` · ${error.digest}` : "");

  // 最小化 HTML/CSS（不依赖 primitives 组件，保证 root layout 场景下也能渲染）。
  return (
    <html lang="zh-CN">
      <head>
        <meta
          name="viewport"
          content="width=device-width, initial-scale=1, viewport-fit=cover"
        />
      </head>
      <body
        style={{
          margin: 0,
          padding:
            "max(1rem, env(safe-area-inset-top, 0px)) max(1rem, env(safe-area-inset-right, 0px)) max(1rem, env(safe-area-inset-bottom, 0px)) max(1rem, env(safe-area-inset-left, 0px))",
          minWidth: 320,
          minHeight: "100svh",
          boxSizing: "border-box",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: "var(--bg-0, #09090B)",
          color: "var(--fg-0, #F5F2EB)",
          fontFamily:
            'var(--font-body, "Geist", "SourceHanSans-VF", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif)',
        }}
      >
        <div
          style={{
            width: "100%",
            maxWidth: 480,
            padding: "clamp(1rem, 5vw, 2rem)",
            boxSizing: "border-box",
            textAlign: "center",
          }}
        >
          <h1
            style={{
              fontSize: "1.5rem",
              fontWeight: 600,
              marginBottom: "0.75rem",
              color: "var(--fg-0, #F5F2EB)",
              letterSpacing: 0,
              lineHeight: 1.1,
            }}
          >
            页面出错
          </h1>
          <p
            style={{
              fontSize: "0.9375rem",
              color: "var(--fg-1, #C2BCAF)",
              marginBottom: "1.25rem",
              lineHeight: 1.5,
            }}
          >
            刷新页面，或先返回首页。
          </p>
          {detail && (
            <pre
              style={{
                fontSize: "0.75rem",
                color: "var(--fg-2, #8A8378)",
                marginBottom: "1.25rem",
                padding: "0.75rem",
                borderRadius: "var(--radius-card, 8px)",
                border: "1px solid var(--border, rgba(245,242,235,0.13))",
                backgroundColor: "var(--bg-1, #0D0E11)",
                wordBreak: "break-all",
                whiteSpace: "pre-wrap",
                overflowWrap: "anywhere",
                textAlign: "left",
                fontFamily:
                  'var(--font-mono, "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace)',
              }}
            >
              {detail}
            </pre>
          )}
          <div
            style={{
              display: "flex",
              gap: "0.75rem",
              justifyContent: "center",
              flexWrap: "wrap",
            }}
          >
            <button
              type="button"
              onClick={handleRetry}
              style={{
                minHeight: 44,
                minWidth: 132,
                flex: "1 1 132px",
                padding: "0.5rem 1.25rem",
                borderRadius: "var(--radius-control, 6px)",
                border: "1px solid var(--accent-border, #74522A)",
                backgroundColor: "var(--accent, #F2A93A)",
                color: "var(--accent-on, #18120A)",
                fontSize: "0.875rem",
                cursor: "pointer",
                fontFamily: "inherit",
                fontWeight: 540,
              }}
            >
              重试
            </button>
            <Link
              href="/"
              style={{
                minHeight: 44,
                minWidth: 132,
                flex: "1 1 132px",
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                padding: "0.5rem 1.25rem",
                borderRadius: "var(--radius-control, 6px)",
                border: "1px solid var(--border, rgba(245,242,235,0.13))",
                backgroundColor: "var(--bg-2, #121318)",
                color: "var(--fg-1, #C2BCAF)",
                fontSize: "0.875rem",
                textDecoration: "none",
                fontFamily: "inherit",
                fontWeight: 540,
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
