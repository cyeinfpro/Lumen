"use client";

// 移动端创作 Tab 空态：Darkroom hero + 建议卡片。
// 点击卡片 → onPick(text, mode) + dispatch "lumen:composer-expand" 事件。

import { useState } from "react";
import { AlertTriangle, ArrowRight, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { useChatStore } from "@/store/useChatStore";
import { cn } from "@/lib/utils";
import { isAbortLike, errorMessage } from "@/lib/errorUtils";

type ComposerMode = "chat" | "image";
type LoadHistoricalMessages = (
  convId: string,
  loadMore?: boolean,
) => Promise<void> | void;

interface Suggestion {
  text: string;
  mode: ComposerMode;
}

interface HistoryStoreExtras {
  messagesLoading?: boolean;
  isLoadingMessages?: boolean;
  historyLoading?: boolean;
  historicalMessagesLoading?: boolean;
  messagesError?: unknown;
  messagesLoadError?: unknown;
  historyError?: unknown;
  historicalMessagesError?: unknown;
}

const SUGGESTIONS: Suggestion[] = [
  { text: "傍晚海边，镜头略俯，暖色调", mode: "image" },
  { text: "戴眼镜的橘猫，水彩质感", mode: "image" },
  { text: "雨夜东京街头，霓虹倒映在地面", mode: "image" },
  { text: "帮我把这张照片调成胶片感", mode: "chat" },
  { text: "分析这张图的构图和光影", mode: "chat" },
  { text: "用克制一点的语言描述这张照片", mode: "chat" },
];

export function MobileEmptyStudio({
  onPick,
}: {
  onPick: (text: string, mode: ComposerMode) => void;
}) {
  const currentConvId = useChatStore((s) => s.currentConvId);
  const loadHistoricalMessages = useChatStore(
    (s) => s.loadHistoricalMessages as LoadHistoricalMessages,
  );
  const storeLoading = useChatStore((s) => {
    const extra = s as unknown as HistoryStoreExtras;
    return Boolean(
      extra.messagesLoading ??
        extra.isLoadingMessages ??
        extra.historyLoading ??
        extra.historicalMessagesLoading,
    );
  });
  const storeError = useChatStore((s) => {
    const extra = s as unknown as HistoryStoreExtras;
    return errorMessage(
      extra.messagesError ??
        extra.messagesLoadError ??
        extra.historyError ??
        extra.historicalMessagesError,
    );
  });
  const [fallbackLoading, setFallbackLoading] = useState(false);
  const [fallbackError, setFallbackError] = useState<string | null>(null);
  const loading = storeLoading || fallbackLoading;
  const error = storeError ?? fallbackError;

  const handlePick = (s: Suggestion) => {
    onPick(s.text, s.mode);
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("lumen:composer-expand"));
    }
  };

  const handleRetryHistory = async () => {
    if (!currentConvId || loading) return;
    setFallbackLoading(true);
    setFallbackError(null);
    try {
      await loadHistoricalMessages(currentConvId, false);
    } catch (err) {
      if (!isAbortLike(err)) {
        setFallbackError(errorMessage(err) ?? "消息加载失败，重试");
      }
    } finally {
      setFallbackLoading(false);
    }
  };

  const imageSuggestions = SUGGESTIONS.filter((s) => s.mode === "image");
  const chatSuggestions = SUGGESTIONS.filter((s) => s.mode === "chat");

  return (
    <div className="flex flex-col items-stretch px-1 pb-10 pt-12">
      {/* Hero */}
      <div className="mb-10">
        <div className="mb-4 flex items-center gap-2.5">
          <span
            className={cn(
              "inline-flex h-9 w-9 items-center justify-center rounded-[var(--radius-panel)]",
              "bg-accent-soft",
            )}
          >
            <Sparkles className="h-[18px] w-[18px] text-accent" />
          </span>
          <h1 className="type-display-lg">
            Lumen
          </h1>
        </div>
        <p className="type-body max-w-[280px] text-[var(--fg-1)]">
          先写一句话。
        </p>
      </div>

      {error ? (
        <div
          role="alert"
          className={cn(
            "mb-4 flex items-center gap-2 rounded-[var(--radius-panel)] border px-3 py-2.5",
            "border-danger-border bg-danger-soft type-body-sm text-[var(--fg-0)]",
          )}
        >
          <AlertTriangle
            className="h-4 w-4 shrink-0 text-[var(--danger)]"
            aria-hidden
          />
          <span className="min-w-0 flex-1 truncate">{error}</span>
          <Button
            size="sm"
            variant="outline"
            loading={loading}
            onClick={() => {
              void handleRetryHistory();
            }}
            className="h-8 shrink-0 px-3"
          >
            重试
          </Button>
        </div>
      ) : loading && currentConvId ? (
        <div className="mb-4 text-center type-body-sm text-[var(--fg-2)]">
          历史消息加载中…
        </div>
      ) : null}

      {/* 生图建议 — 2 列网格 */}
      <div className="mb-8">
        <div className="mb-3 flex items-center gap-1.5 type-caption text-[var(--fg-2)]">
          <span className="h-1.5 w-1.5 rounded-full bg-[var(--fg-3)]" aria-hidden />
          图片
        </div>
        <div className="grid grid-cols-2 gap-2.5">
          {imageSuggestions.map((s) => (
            <Button
              key={`img:${s.text}`}
              variant="secondary"
              size="md"
              onClick={() => handlePick(s)}
              className={cn(
                "group relative h-auto w-full justify-start px-3.5 py-3.5 text-left",
                "rounded-[var(--radius-card)] border border-[var(--border-subtle)]",
                "bg-[var(--bg-1)] type-body-sm text-[var(--fg-0)]",
                "active:scale-[0.98] transition-[transform,border-color,background-color] duration-150",
                "hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]",
              )}
              style={{ fontFamily: "var(--font-zh-body)" }}
            >
              <span className="flex flex-col gap-2.5">
                <span className="type-overline text-[var(--fg-3)]">图像</span>
                <span className="min-w-0 break-words type-body-sm leading-snug text-[var(--fg-1)]">{s.text}</span>
              </span>
              <ArrowRight
                aria-hidden
                className="absolute top-3.5 right-3 h-3 w-3 text-[var(--fg-3)] transition-colors group-hover:text-[var(--fg-1)]"
              />
            </Button>
          ))}
        </div>
      </div>

      {/* 对话建议 — 单列 */}
      <div>
        <div className="mb-3 flex items-center gap-1.5 type-caption text-[var(--fg-2)]">
          <span className="h-1.5 w-1.5 rounded-full bg-[var(--fg-3)]" aria-hidden />
          对话
        </div>
        <ul className="flex flex-col gap-2.5">
          {chatSuggestions.map((s) => (
            <li key={`ask:${s.text}`}>
              <Button
                variant="secondary"
                size="md"
                onClick={() => handlePick(s)}
                className={cn(
                  "group relative h-auto w-full justify-start px-4 py-3.5 text-left",
                  "rounded-[var(--radius-card)] border border-[var(--border-subtle)]",
                  "bg-[var(--bg-1)] type-body text-[var(--fg-0)]",
                  "active:scale-[0.995] transition-[transform,border-color] duration-150",
                  "hover:border-[var(--border-strong)]",
                )}
                style={{ fontFamily: "var(--font-zh-body)" }}
              >
                <span className="flex items-center gap-3">
                  <span className="type-overline shrink-0 text-[var(--fg-3)]">提问</span>
                  <span className="flex-1 min-w-0 break-words type-body-sm leading-snug text-[var(--fg-1)]">{s.text}</span>
                  <ArrowRight
                    aria-hidden
                    className="h-3.5 w-3.5 shrink-0 text-[var(--fg-3)] transition-colors group-hover:text-[var(--fg-1)]"
                  />
                </span>
              </Button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
