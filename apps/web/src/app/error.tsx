"use client";

// Next.js 16 约定：app/error.tsx 是 root segment 的 error boundary。
// 必须是 Client Component；接收 error + reset (兼容旧 v15) 与 unstable_retry (v16+)。
// 我们优先调用 unstable_retry，未注入时回退到 reset 或 window.location.reload。

import { useEffect } from "react";
import Link from "next/link";
import { ErrorState, Button } from "@/components/ui/primitives";
import { Home } from "lucide-react";
import { logError } from "@/lib/logger";

interface ErrorPageProps {
  error: Error & { digest?: string };
  reset?: () => void;
  unstable_retry?: () => void;
}

export default function GlobalError({
  error,
  reset,
  unstable_retry,
}: ErrorPageProps) {
  useEffect(() => {
    logError(error, {
      scope: "app/error",
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
        // 沙箱（iframe / 部分嵌入式 webview）会阻止脚本调用 reload；提示用户手动刷新
        if (typeof window !== "undefined" && typeof window.alert === "function") {
          window.alert("当前环境不允许自动刷新，请手动刷新页面");
        }
      }
    }
  };

  const detail =
    (error.message || "未知错误") + (error.digest ? ` · ${error.digest}` : "");

  return (
    <div className="min-h-[100dvh] w-full flex-1 flex items-center justify-center bg-[var(--bg-0)] px-4 sm:px-6 safe-area">
      <div className="w-full max-w-md">
        <ErrorState
          title="页面出了点问题"
          description="这次请求出错了。可以试着重试，或返回首页。"
          detail={detail}
          onRetry={handleRetry}
          retryLabel="重试"
          secondaryAction={
            <Link href="/">
              <Button
                variant="ghost"
                size="sm"
                leftIcon={<Home className="w-3.5 h-3.5" />}
              >
                返回首页
              </Button>
            </Link>
          }
        />
      </div>
    </div>
  );
}
