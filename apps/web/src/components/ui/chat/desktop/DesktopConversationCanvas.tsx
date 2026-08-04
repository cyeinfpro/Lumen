"use client";

// 桌面端创作画布：单一内容轴 + Scene 分隔 + DevelopingCard 显影扫光。
// 按 messages 顺序两两配对（user → assistant），渲染 Scene NN 分隔条。
// 跟移动端 MobileConversationCanvas 设计哲学一致，差异：
//   - 桌面端提示词、文本和单图统一到 760px 内容轴
//   - 单图按视口高度限制，优先完整显示
//   - 右键 / hover"···" 触发上下文菜单（移动端长按）
//   - 保留虚拟化（messages > 80）

import {
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useHistoryPaging } from "@/hooks/useHistoryPaging";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, ArrowDown } from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Button, IconButton } from "@/components/ui/primitives";
import { useChatStore } from "@/store/useChatStore";
import { cn } from "@/lib/utils";
import type {
  AssistantMessage,
  Generation,
  Intent,
  Message,
  UserMessage,
} from "@/lib/types";
import { DesktopSceneDivider } from "./DesktopSceneDivider";
import {
  AssistantTurn,
  UserTurn,
  generationSignature,
  latestAssistantIsStreaming,
  messageScrollSignature,
} from "./DesktopConversationTurns";
import { ImageContextMenu } from "./DesktopConversationImageMenu";

const EASE_OUT_EXPO = [0.16, 1, 0.3, 1] as const;
const STICK_TO_BOTTOM_PX = 120;
// 与移动会话画布一致：从 80 降到 50（P2-UX）。
const VIRTUALIZE_AFTER = 50;

interface DesktopConversationCanvasProps {
  messages: Message[];
  generations: Record<string, Generation>;
  scrollRef: RefObject<HTMLDivElement | null>;
  onEditImage: (imageId: string) => void;
  onRetryGen: (generationId: string) => void;
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
  anchorId: string;
}

interface ImageMenuInfo {
  imageId: string;
  prompt: string;
  genId: string;
  x: number;
  y: number;
}

function pairScenes(messages: Message[]): SceneEntry[] {
  const scenes: SceneEntry[] = [];
  let i = 0;
  let idx = 0;
  while (i < messages.length) {
    const m = messages[i];
    if (m.role === "user") {
      const next = messages[i + 1];
      const assistant = next && next.role === "assistant" ? next : null;
      idx += 1;
      scenes.push({ index: idx, user: m, assistant, anchorId: m.id });
      i += assistant ? 2 : 1;
    } else {
      idx += 1;
      scenes.push({ index: idx, user: null, assistant: m, anchorId: m.id });
      i += 1;
    }
  }
  return scenes;
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
    <div ref={sentinelRef} className="relative z-[var(--z-base)] flex justify-center pb-2">
      {error ? (
        <div
          role="alert"
          className={cn(
            "flex max-w-full items-center gap-2 rounded-[var(--radius-control)] border px-2.5 py-1.5",
            "border-danger-border bg-danger-soft type-caption text-[var(--fg-0)]",
          )}
        >
          <AlertTriangle
            className="h-4 w-4 shrink-0 text-[var(--danger)]"
            aria-hidden
          />
          <span className="min-w-0 truncate">{error}</span>
          <Button
            size="sm"
            variant="outline"
            loading={loading}
            onClick={onRetry}
            className="shrink-0"
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
          className="text-[var(--fg-2)]"
        >
          {loading ? "正在加载" : "加载更早消息"}
        </Button>
      )}
    </div>
  );
}

export function DesktopConversationCanvas({
  messages,
  generations,
  scrollRef,
  onEditImage,
  onRetryGen,
  onRetryText,
  onRegenerate,
}: DesktopConversationCanvasProps) {
  const router = useRouter();
  const currentConvId = useChatStore((s) => s.currentConvId);
  const scenes = useMemo(() => pairScenes(messages), [messages]);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [menuInfo, setMenuInfo] = useState<ImageMenuInfo | null>(null);
  const historyPaging = useHistoryPaging(messages.length, {
    scrollRef,
    rootMargin: "120px 0px 0px 0px",
  });

  const shouldVirtualize = messages.length > VIRTUALIZE_AFTER;
  const stickToBottomRef = useRef(true);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const genSignature = useMemo(
    () => generationSignature(generations),
    [generations],
  );
  const scrollSignature = useMemo(
    () => messageScrollSignature(messages),
    [messages],
  );
  const latestIsStreaming = useMemo(
    () => latestAssistantIsStreaming(messages),
    [messages],
  );

  const toggleCollapse = useCallback((anchorId: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(anchorId)) next.delete(anchorId);
      else next.add(anchorId);
      return next;
    });
  }, []);

  const handleOpenMenu = useCallback((info: ImageMenuInfo) => {
    setMenuInfo(info);
  }, []);

  // stick-to-bottom：滚动监听
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      stickToBottomRef.current = distance < STICK_TO_BOTTOM_PX;
      const shouldShow = distance > STICK_TO_BOTTOM_PX * 2;
      setShowJumpToLatest((prev) => (prev === shouldShow ? prev : shouldShow));
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => el.removeEventListener("scroll", onScroll);
  }, [scrollRef]);

  useEffect(() => {
    stickToBottomRef.current = true;
  }, [currentConvId]);

  // 虚拟化：场景级（每个 scene 一行）
  // eslint-disable-next-line react-hooks/incompatible-library
  const rowVirtualizer = useVirtualizer({
    count: scenes.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 360,
    overscan: 4,
    enabled: shouldVirtualize,
  });

  const scrollToLatest = useCallback(
    (behavior: ScrollBehavior = "smooth") => {
      const el = scrollRef.current;
      if (!el) return;

      const run = (mode: ScrollBehavior) => {
        if (shouldVirtualize && scenes.length > 0) {
          rowVirtualizer.scrollToIndex(scenes.length - 1, { align: "end" });
        }
        el.scrollTo({ top: el.scrollHeight, behavior: mode });
      };

      stickToBottomRef.current = true;
      setShowJumpToLatest(false);
      requestAnimationFrame(() => {
        run(behavior);
        requestAnimationFrame(() => run("auto"));
      });
      window.setTimeout(() => run("auto"), 140);
    },
    [rowVirtualizer, scenes.length, scrollRef, shouldVirtualize],
  );

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickToBottomRef.current || scenes.length === 0) return;
    const prefersReduced =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    scrollToLatest(prefersReduced || latestIsStreaming ? "auto" : "smooth");
  }, [
    currentConvId,
    genSignature,
    latestIsStreaming,
    scrollRef,
    scrollToLatest,
    scrollSignature,
    scenes.length,
  ]);

  const renderScene = useCallback(
    (scene: SceneEntry): ReactNode => {
      const isCollapsed = collapsed.has(scene.anchorId);
      return (
        <section
          key={scene.anchorId}
          id={`scene-${scene.anchorId}`}
          data-history-scroll-anchor={scene.anchorId}
          aria-label={`Scene ${String(scene.index).padStart(2, "0")}`}
          className="relative py-2"
          style={
            shouldVirtualize
              ? undefined
              : {
                  contentVisibility: "auto",
                  containIntrinsicSize: "360px",
                }
          }
        >
          <DesktopSceneDivider
            index={scene.index}
            collapsed={isCollapsed}
            controlsId={`scene-content-${scene.anchorId}`}
            onToggle={() => toggleCollapse(scene.anchorId)}
          />
          <div id={`scene-content-${scene.anchorId}`}>
            {!isCollapsed && (
              <div className="flex flex-col gap-4 px-2 pb-5">
                {scene.user && <UserTurn msg={scene.user} />}
                {scene.assistant && (
                  <AssistantTurn
                    msg={scene.assistant}
                    generations={generations}
                    onEditImage={onEditImage}
                    onRetryGen={onRetryGen}
                    onRetryText={onRetryText}
                    onRegenerate={onRegenerate}
                    onOpenMenu={handleOpenMenu}
                  />
                )}
              </div>
            )}
          </div>
        </section>
      );
    },
    [
      collapsed,
      generations,
      handleOpenMenu,
      onEditImage,
      onRegenerate,
      onRetryGen,
      onRetryText,
      shouldVirtualize,
      toggleCollapse,
    ],
  );

  const body = shouldVirtualize ? (
    <motion.div
      key="messages-virtual"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.18, ease: EASE_OUT_EXPO }}
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
    </motion.div>
  ) : (
    <motion.div
      key="messages"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.18, ease: EASE_OUT_EXPO }}
      className="flex flex-col"
    >
      <AnimatePresence initial={false}>
        {scenes.map((scene) => renderScene(scene))}
      </AnimatePresence>
    </motion.div>
  );

  return (
    <div
      role="log"
      aria-live="polite"
      aria-relevant="additions"
      className="relative mx-auto w-full max-w-[var(--content-media)]"
    >
      <HistoryLoadControl
        sentinelRef={historyPaging.topSentinelRef}
        hasMore={historyPaging.hasMore}
        loading={historyPaging.loading}
        error={historyPaging.error}
        onLoadMore={historyPaging.loadMore}
        onRetry={historyPaging.retry}
      />

      {body}

      <ImageContextMenu
        info={menuInfo}
        onClose={() => setMenuInfo(null)}
        onEditImage={onEditImage}
        onRetryGen={onRetryGen}
        onLocate={(imageId) =>
          router.push(`/stream?highlight=${imageId}`)
        }
      />

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
      className="pointer-events-none fixed bottom-[calc(84px+env(safe-area-inset-bottom,0px))] z-[var(--z-tray)] -translate-x-1/2"
      style={{
        left: "calc(50% + var(--studio-sidebar-offset, 0px) / 2)",
      }}
    >
      <IconButton
        size="sm"
        variant="secondary"
        aria-label="回到最新"
        tooltip="回到最新"
        onClick={onClick}
        className="pointer-events-auto rounded-full border-[var(--border)] bg-[var(--bg-1)]/92 shadow-[var(--shadow-2)] backdrop-blur-xl"
      >
        <ArrowDown className="h-4 w-4" aria-hidden />
      </IconButton>
    </div>
  );
}
