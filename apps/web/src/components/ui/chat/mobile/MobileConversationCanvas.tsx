"use client";

// Darkroom 移动端画布：无气泡 + Scene 胶片竖线（距左 12px）。
// 按 messages 顺序两两配对（user → assistant），渲染 Scene NN 分隔条。

import {
  type RefObject,
  memo,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  AlertTriangle,
  ArrowDownToLine,
  Copy,
  Check,
  RotateCcw,
} from "lucide-react";
import { Button, IconButton } from "@/components/ui/primitives";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { Markdown } from "@/components/ui/Markdown";
import { cn } from "@/lib/utils";
import { tryCopyTextToClipboard } from "@/lib/clipboard";
import { CompletionStatusLine } from "@/components/ui/chat/CompletionStatusLine";
import {
  ConversationTurn,
  ConversationUserTurn,
} from "@/components/ui/chat/ConversationVisualAtoms";
import { generationRenderSignature } from "@/components/ui/chat/generationRenderSignature";
import { useHistoryPaging } from "@/hooks/useHistoryPaging";

import type {
  AssistantMessage,
  Generation,
  Intent,
  Message,
  UserMessage,
} from "@/lib/types";
import { cancelTask } from "@/lib/apiClient";
import { DevelopingCard } from "./DevelopingCard";
import { SceneDivider } from "./SceneDivider";
import { FinalImage } from "./MobileConversationImage";

const VIRTUALIZE_AFTER = 50;

interface MobileConversationCanvasProps {
  messages: Message[];
  generations: Record<string, Generation>;
  scrollRef?: RefObject<HTMLDivElement | null>;
  scrollToMessageId?: string | null;
  onEditImage: (imageId: string) => void;
  onRetryGen: (gid: string) => void;
  onRetryText: (assistantId: string) => void;
  onRegenerate: (
    assistantId: string,
    intent?: Exclude<Intent, "auto">,
  ) => void | Promise<void>;
}

interface SceneEntry {
  index: number;
  user: UserMessage | null;
  assistant: AssistantMessage | null;
  // 用于锚点/折叠态 key：优先 user id，其次 assistant id
  anchorId: string;
}

function pairScenes(messages: Message[]): SceneEntry[] {
  const scenes: SceneEntry[] = [];
  let i = 0;
  let idx = 0;
  while (i < messages.length) {
    const m = messages[i];
    if (m.role === "user") {
      const next = messages[i + 1];
      const assistant =
        next && next.role === "assistant" ? next : null;
      idx += 1;
      scenes.push({
        index: idx,
        user: m,
        assistant,
        anchorId: m.id,
      });
      i += assistant ? 2 : 1;
    } else {
      // 孤立 assistant（比如历史只剩一条）：单独一个 Scene
      idx += 1;
      scenes.push({
        index: idx,
        user: null,
        assistant: m,
        anchorId: m.id,
      });
      i += 1;
    }
  }
  return scenes;
}

function generationIdsOf(msg: AssistantMessage): string[] {
  if (msg.generation_ids?.length) return msg.generation_ids;
  return msg.generation_id ? [msg.generation_id] : [];
}

function assistantGenerationsRenderSignature(
  msg: AssistantMessage,
  generations: Record<string, Generation>,
): string {
  return generationIdsOf(msg)
    .map((id) => generationRenderSignature(generations[id]))
    .join("|");
}

function HistoryLoadControl({
  sentinelRef,
  hasMore,
  loading,
  error,
  onLoadMore,
  onRetry,
}: {
  sentinelRef: RefObject<HTMLDivElement | null>;
  hasMore: boolean;
  loading: boolean;
  error: string | null;
  onLoadMore: () => void;
  onRetry: () => void;
}) {
  if (!hasMore && !loading && !error) return null;

  return (
    <div ref={sentinelRef} className="relative z-[var(--z-base)] flex justify-center pb-1.5">
      {error ? (
        <div
          role="alert"
          className={cn(
            "flex max-w-full items-center gap-2 rounded-[var(--radius-control)] border px-2.5 py-2",
            "border-danger-border bg-danger-soft type-caption text-[var(--fg-0)]",
          )}
        >
          <AlertTriangle
            className="h-3.5 w-3.5 shrink-0 text-[var(--danger)]"
            aria-hidden
          />
          <span className="min-w-0 truncate">{error}</span>
          <Button
            size="sm"
            variant="outline"
            loading={loading}
            onClick={onRetry}
            className="min-h-11 shrink-0 px-3"
          >
            重试
          </Button>
        </div>
      ) : (
        <Button
          size="sm"
          variant="ghost"
          loading={loading}
          onClick={onLoadMore}
          disabled={!hasMore && !loading}
          className="min-h-11 text-[var(--fg-2)]"
        >
          {loading ? "加载中" : "加载更早消息"}
        </Button>
      )}
    </div>
  );
}

export function MobileConversationCanvas({
  messages,
  generations,
  scrollRef,
  scrollToMessageId,
  onEditImage,
  onRetryGen,
  onRetryText,
  onRegenerate,
}: MobileConversationCanvasProps) {
  const scenes = useMemo(() => pairScenes(messages), [messages]);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const historyPaging = useHistoryPaging(messages.length, {
    scrollRef,
    rootMargin: "96px 0px 0px 0px",
  });
  const shouldVirtualize = messages.length > VIRTUALIZE_AFTER;

  // eslint-disable-next-line react-hooks/incompatible-library
  const rowVirtualizer = useVirtualizer({
    count: scenes.length,
    getScrollElement: () => scrollRef?.current ?? null,
    estimateSize: () => 330,
    overscan: 4,
    enabled: shouldVirtualize,
  });
  const scrollTargetKey = useMemo(() => {
    if (!scrollToMessageId) return null;
    const sceneIndex = scenes.findIndex(
      (scene) =>
        scene.user?.id === scrollToMessageId ||
        scene.assistant?.id === scrollToMessageId,
    );
    if (sceneIndex < 0) return null;
    return `${sceneIndex}:${scenes[sceneIndex].anchorId}`;
  }, [scenes, scrollToMessageId]);

  const toggleCollapse = useCallback((anchorId: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(anchorId)) next.delete(anchorId);
      else next.add(anchorId);
      return next;
    });
  }, []);

  const scrollToLatest = useCallback(
    (behavior: ScrollBehavior = "smooth") => {
      const el = scrollRef?.current;
      if (!el) return;
      setShowJumpToLatest(false);
      requestAnimationFrame(() => {
        el.scrollTo({ top: el.scrollHeight, behavior });
        requestAnimationFrame(() => {
          el.scrollTo({ top: el.scrollHeight, behavior: "auto" });
        });
      });
      window.setTimeout(() => {
        el.scrollTo({ top: el.scrollHeight, behavior: "auto" });
      }, 140);
    },
    [scrollRef],
  );

  useEffect(() => {
    const el = scrollRef?.current;
    if (!el) return;

    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      const shouldShow = distance > 240;
      setShowJumpToLatest((prev) => (prev === shouldShow ? prev : shouldShow));
    };

    el.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => el.removeEventListener("scroll", onScroll);
  }, [scrollRef]);

  useEffect(() => {
    if (!scrollToMessageId || !scrollTargetKey) return;
    const separatorIndex = scrollTargetKey.indexOf(":");
    const sceneIndex = Number(scrollTargetKey.slice(0, separatorIndex));
    const anchorId = scrollTargetKey.slice(separatorIndex + 1);
    if (!Number.isInteger(sceneIndex) || sceneIndex < 0 || !anchorId) return;

    setCollapsed((prev) => {
      if (!prev.has(anchorId)) return prev;
      const next = new Set(prev);
      next.delete(anchorId);
      return next;
    });

    const scrollIntoView = (behavior: ScrollBehavior): boolean => {
      const messageEl = document.getElementById(`msg-${scrollToMessageId}`);
      if (messageEl) {
        messageEl.scrollIntoView({ behavior, block: "center" });
        return true;
      }
      const sceneEl = document.getElementById(`scene-${anchorId}`);
      if (sceneEl) {
        sceneEl.scrollIntoView({ behavior, block: "center" });
        return true;
      }
      return false;
    };

    if (shouldVirtualize) {
      rowVirtualizer.scrollToIndex(sceneIndex, { align: "center" });
    }

    requestAnimationFrame(() => {
      if (scrollIntoView("smooth")) return;
      requestAnimationFrame(() => {
        void scrollIntoView("smooth");
      });
    });
    const fallback = window.setTimeout(() => {
      void scrollIntoView("auto");
    }, 180);
    return () => window.clearTimeout(fallback);
  }, [
    rowVirtualizer,
    scrollToMessageId,
    scrollTargetKey,
    shouldVirtualize,
  ]);

  const renderScene = useCallback(
    (scene: SceneEntry) => {
      const isCollapsed = collapsed.has(scene.anchorId);
      return (
        <section
          key={scene.anchorId}
          id={`scene-${scene.anchorId}`}
          data-history-scroll-anchor={scene.anchorId}
          aria-label={`Scene ${String(scene.index).padStart(2, "0")}`}
          className="relative"
          style={
            shouldVirtualize
              ? undefined
              : {
                  contentVisibility: "auto",
                  containIntrinsicSize: "320px",
                }
          }
        >
          <SceneDivider
            index={scene.index}
            collapsed={isCollapsed}
            onToggle={() => toggleCollapse(scene.anchorId)}
          />
          {!isCollapsed && (
            <div className="flex flex-col gap-3 pl-7 pr-0.5 pb-3">
              {scene.user && <UserTurn msg={scene.user} />}
              {scene.assistant && (
                <AssistantTurn
                  msg={scene.assistant}
                  generations={generations}
                  onEditImage={onEditImage}
                  onRetryGen={onRetryGen}
                  onRetryText={onRetryText}
                  onRegenerate={onRegenerate}
                />
              )}
            </div>
          )}
        </section>
      );
    },
    [
      collapsed,
      generations,
      onEditImage,
      onRegenerate,
      onRetryGen,
      onRetryText,
      shouldVirtualize,
      toggleCollapse,
    ],
  );

  const body = shouldVirtualize ? (
    <div
      className="relative w-full"
      style={{ height: rowVirtualizer.getTotalSize() }}
    >
      {rowVirtualizer.getVirtualItems().map((virtualRow) => {
        const scene = scenes[virtualRow.index];
        return (
          <div
            key={scene.anchorId}
            ref={rowVirtualizer.measureElement}
            data-index={virtualRow.index}
            className="absolute left-0 top-0 w-full"
            style={{ transform: `translateY(${virtualRow.start}px)` }}
          >
            {renderScene(scene)}
          </div>
        );
      })}
    </div>
  ) : (
    <div className="flex flex-col">{scenes.map((scene) => renderScene(scene))}</div>
  );

  return (
    <div
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      className="relative"
    >
      {/* 贯穿竖线 */}
      <div
        aria-hidden
        className="pointer-events-none absolute top-0 bottom-0 w-px bg-gradient-to-b from-transparent via-[var(--border-subtle)] to-transparent"
        style={{ left: "12px" }}
      />

      <HistoryLoadControl
        sentinelRef={historyPaging.topSentinelRef}
        hasMore={historyPaging.hasMore}
        loading={historyPaging.loading}
        error={historyPaging.error}
        onLoadMore={historyPaging.loadMore}
        onRetry={historyPaging.retry}
      />

      {body}

      <JumpToLatestButton
        visible={showJumpToLatest}
        onClick={() => scrollToLatest("smooth")}
      />
    </div>
  );
}

function JumpToLatestButton({
  visible,
  onClick,
}: {
  visible: boolean;
  onClick: () => void;
}) {
  if (!visible) return null;

  return (
    <div
      className="fixed left-1/2 z-[var(--z-tray)] -translate-x-1/2"
      style={{ bottom: "calc(var(--bottom-overlay-stack, 120px) + 4px)" }}
    >
      <Button
        size="sm"
        variant="secondary"
        leftIcon={<ArrowDownToLine className="h-3.5 w-3.5" aria-hidden />}
        onClick={onClick}
        className="min-h-11 border-[var(--border)] bg-[var(--bg-1)]/90 px-3 shadow-[var(--shadow-2)] backdrop-blur-xl"
      >
        最新
      </Button>
    </div>
  );
}

// ———————————————————————————————————————————————————
// 用户 turn：与桌面共用同一内容轴、附件、描边和复制操作。
// ———————————————————————————————————————————————————
const UserTurn = memo(function UserTurn({ msg }: { msg: UserMessage }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    if (!msg.text) return;
    void tryCopyTextToClipboard(msg.text).then((success) => {
      if (!success) {
        pushMobileToast("复制失败", "danger");
        return;
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    });
  };

  return <ConversationUserTurn msg={msg} copied={copied} onCopy={copy} />;
});

// ———————————————————————————————————————————————————
// 助手 turn：左对齐 Markdown + 生成图 + 参数尾行
// ———————————————————————————————————————————————————
interface AssistantTurnProps {
  msg: AssistantMessage;
  generations: Record<string, Generation>;
  onEditImage: (imageId: string) => void;
  onRetryGen: (gid: string) => void;
  onRetryText: (assistantId: string) => void;
  onRegenerate: (
    assistantId: string,
    intent?: Exclude<Intent, "auto">,
  ) => void | Promise<void>;
}

function isChatLikeAssistantMessage(msg: AssistantMessage): boolean {
  return msg.intent_resolved === "chat" || msg.intent_resolved === "vision_qa";
}

function deriveAssistantTurnState(
  msg: AssistantMessage,
  generations: Record<string, Generation>,
) {
  const gens = generationIdsOf(msg)
    .map((id) => generations[id])
    .filter((generation): generation is Generation => Boolean(generation));
  const isStreaming = msg.status === "streaming";
  const isFailedText =
    msg.status === "failed" && isChatLikeAssistantMessage(msg);
  return {
    gens,
    isStreaming,
    isFailedText,
    canCopy: Boolean(msg.text && msg.status !== "pending"),
    canRegenerate:
      msg.status === "succeeded" &&
      gens.length > 0 &&
      gens.every((generation) => generation.status === "succeeded"),
  };
}

const AssistantTurn = memo(function AssistantTurn({
  msg,
  generations,
  onEditImage,
  onRetryGen,
  onRetryText,
  onRegenerate,
}: AssistantTurnProps) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    if (!msg.text) return;
    void tryCopyTextToClipboard(msg.text).then((success) => {
      if (!success) {
        pushMobileToast("复制失败", "danger");
        return;
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    });
  };

  const {
    gens,
    isStreaming,
    isFailedText,
    canCopy,
    canRegenerate,
  } = deriveAssistantTurnState(msg, generations);

  return (
    <ConversationTurn
      id={`msg-${msg.id}`}
      side="assistant"
      className="flex flex-col gap-2.5"
    >
      <CompletionStatusLine msg={msg} compact />

      {/* 助手正文 */}
      {(msg.text || isFailedText) && (
        <div className="flex items-start gap-2">
          <div
            className={cn(
              "type-body min-w-0 flex-1 break-words [overflow-wrap:anywhere]",
              "text-[var(--fg-0)]",
              "[&_pre]:max-w-full [&_pre]:overflow-x-auto [&_pre]:overscroll-x-contain [&_code]:break-words [&_table]:block [&_table]:max-w-full [&_table]:overflow-x-auto [&_img]:max-w-full [&_img]:h-auto",
              isFailedText && "text-[var(--danger)]",
            )}
            style={{ fontFamily: "var(--font-body)" }}
          >
            {msg.text ? (
              <Markdown autoDetectCode={!isStreaming}>{msg.text}</Markdown>
            ) : null}
            {isStreaming && (
              <span
                aria-hidden
                className="ml-0.5 inline-block w-[0.5ch] animate-pulse text-accent motion-reduce:animate-none"
              >
                ▍
              </span>
            )}
          </div>
          {canCopy && (
            <IconButton
              size="sm"
              onClick={copy}
              aria-label={copied ? "已复制" : "复制"}
              tooltip={copied ? "已复制" : "复制"}
              className="mt-0.5 text-[var(--fg-3)]"
            >
              {copied ? <Check className="w-3.5 h-3.5 text-success" /> : <Copy className="w-3.5 h-3.5" />}
            </IconButton>
          )}
        </div>
      )}

      {/* 文本失败重试 */}
      {isFailedText && (
        <Button
          size="sm"
          variant="secondary"
          onClick={() => onRetryText(msg.id)}
          className="self-start min-h-11 px-3"
          aria-label="重试"
          leftIcon={<RotateCcw className="h-3.5 w-3.5" aria-hidden />}
        >
          重试
        </Button>
      )}

      {/* 已完成的助手消息：提供重新生成按钮 */}
      {canRegenerate && (
        <div className="flex items-center gap-2 pt-0.5">
          <Button
            size="sm"
            variant="outline"
            onClick={() => onRegenerate(msg.id, msg.intent_resolved)}
            className="min-h-11 px-3 text-[var(--fg-2)]"
            leftIcon={<RotateCcw className="h-3.5 w-3.5" aria-hidden />}
          >
            重新生成
          </Button>
        </div>
      )}

      {gens.length > 0 && (
        <div
          className={cn(
            gens.length === 1
              ? "flex flex-col gap-1.5"
              : "grid w-full max-w-[420px] grid-cols-2 gap-2",
          )}
        >
          {gens.map((gen) => {
            if (
              gen.status === "queued" ||
              gen.status === "running" ||
              gen.status === "failed"
            ) {
              return (
                <DevelopingCard
                  key={gen.id}
                  gen={gen}
                  onRetry={onRetryGen}
                  onCancel={(gid) => {
                    void cancelTask("generations", gid).catch(() => {
                      pushMobileToast("取消失败", "danger");
                    });
                  }}
                />
              );
            }
            if (gen.status === "succeeded" && gen.image) {
              return (
                <FinalImage
                  key={gen.id}
                  gen={gen}
                  image={gen.image}
                  onEditImage={onEditImage}
                  inGrid={gens.length > 1}
                />
              );
            }
            return null;
          })}
        </div>
      )}
    </ConversationTurn>
  );
}, areAssistantTurnPropsEqual);

function areAssistantTurnPropsEqual(
  prev: AssistantTurnProps,
  next: AssistantTurnProps,
): boolean {
  if (prev.msg !== next.msg) return false;
  if (
    prev.onEditImage !== next.onEditImage ||
    prev.onRetryGen !== next.onRetryGen ||
    prev.onRetryText !== next.onRetryText ||
    prev.onRegenerate !== next.onRegenerate
  ) {
    return false;
  }
  return (
    assistantGenerationsRenderSignature(prev.msg, prev.generations) ===
    assistantGenerationsRenderSignature(next.msg, next.generations)
  );
}
