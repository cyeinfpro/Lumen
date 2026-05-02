"use client";

// Darkroom 桌面端画布：无气泡 + Scene 胶片竖线 + DevelopingCard 显影扫光。
// 按 messages 顺序两两配对（user → assistant），渲染 Scene NN 分隔条。
// 跟移动端 MobileConversationCanvas 设计哲学一致，差异：
//   - 贯穿竖线距左 24px（移动端 20px）
//   - 内容 pl-10 pr-3（移动端 pl-10 pr-1）
//   - 右键 / hover"···" 触发上下文菜单（移动端长按）
//   - 保留虚拟化（messages > 80）

import {
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  type RefObject,
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useHistoryPaging } from "@/hooks/useHistoryPaging";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion } from "framer-motion";
import {
  AlertTriangle,
  ArrowDownToLine,
  Check,
  Clipboard,
  Copy,
  Download,
  ExternalLink,
  ImagePlus,
  MoreHorizontal,
  RefreshCw,
  RotateCcw,
} from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Button, toast } from "@/components/ui/primitives";
import { Markdown } from "@/components/ui/Markdown";
import { ViewportImage } from "@/components/ui/ViewportImage";
import { useChatStore } from "@/store/useChatStore";
import { useUiStore } from "@/store/useUiStore";
import { cn } from "@/lib/utils";
import { CompletionStatusLine } from "@/components/ui/chat/CompletionStatusLine";
import type {
  AssistantMessage,
  Generation,
  GeneratedImage,
  Intent,
  Message,
  UserMessage,
} from "@/lib/types";
import { imageBinaryUrl, imageVariantUrl } from "@/lib/apiClient";
import { prewarmImage } from "@/lib/imagePreload";
import { aspectRatioToCss } from "@/lib/sizing";
import type { LightboxItem } from "@/components/ui/lightbox/types";
import { DevelopingCard } from "@/components/ui/chat/mobile";
import { DesktopSceneDivider } from "./DesktopSceneDivider";

const EASE_OUT_EXPO = [0.16, 1, 0.3, 1] as const;
const STICK_TO_BOTTOM_PX = 120;
// 与 ConversationCanvas 一致：从 80 降到 50（P2-UX）。
const VIRTUALIZE_AFTER = 50;

interface DesktopConversationCanvasProps {
  messages: Message[];
  generations: Record<string, Generation>;
  scrollRef: React.RefObject<HTMLDivElement | null>;
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

function generationIdsOf(msg: AssistantMessage): string[] {
  if (msg.generation_ids?.length) return msg.generation_ids;
  return msg.generation_id ? [msg.generation_id] : [];
}

function formatElapsed(g: Generation): string | null {
  if (!g.finished_at || !g.started_at) return null;
  const ms = Math.max(0, g.finished_at - g.started_at);
  return `${(Math.round(ms / 100) / 10).toFixed(1)}s`;
}

function aspectRatioNumber(
  image: Pick<GeneratedImage, "width" | "height">,
  fallback: string,
): number | null {
  if (image.width && image.height && image.height > 0) {
    return image.width / image.height;
  }
  const match = fallback.match(/^(\d+)\s*:\s*(\d+)$/);
  if (!match) return null;
  const w = Number(match[1]);
  const h = Number(match[2]);
  return w > 0 && h > 0 ? w / h : null;
}

function singleImageWidthClass(ratio: number | null): string {
  if (ratio !== null && ratio < 0.58) return "max-w-[min(18%,180px)]";
  if (ratio !== null && ratio < 0.9) return "max-w-[min(26%,280px)]";
  if (ratio !== null && ratio > 1.7) return "max-w-[min(38%,440px)]";
  return "max-w-[min(30%,340px)]";
}

function gridWidthClass(count: number): string {
  if (count === 2) return "max-w-[560px]";
  if (count === 3) return "max-w-[720px]";
  if (count === 4) return "max-w-[760px]";
  if (count === 5) return "max-w-[900px]";
  if (count === 6) return "max-w-[960px]";
  return "max-w-[760px]";
}

function openLightbox(items: LightboxItem[], initialId: string) {
  if (typeof window === "undefined") return;
  if (items.length === 0) return;
  // BUG-006/019: 统一使用 Zustand store action 打开灯箱，避免 CustomEvent 竞态。
  useUiStore.getState().openLightboxFromItems(items, initialId);
}

function conversationImageSrc(image: GeneratedImage): string {
  return (
    image.preview_url ??
    image.thumb_url ??
    image.display_url ??
    image.data_url
  );
}

function lightboxThumbUrl(image: GeneratedImage): string | undefined {
  return image.thumb_url ?? image.preview_url;
}

function generationSignature(generations: Record<string, Generation>): string {
  return Object.values(generations)
    .map((g) => `${g.id}:${g.status}:${g.stage}:${g.image?.id ?? ""}`)
    .join("|");
}

function messageScrollSignature(messages: Message[]): string {
  const last = messages[messages.length - 1];
  if (!last) return "empty";

  if (last.role === "assistant") {
    return [
      messages.length,
      last.id,
      last.status,
      last.text?.length ?? 0,
      last.thinking?.length ?? 0,
      last.last_delta_at ?? 0,
      last.generation_id ?? "",
      last.generation_ids?.join(",") ?? "",
    ].join(":");
  }

  return [
    messages.length,
    last.id,
    last.role,
    last.text?.length ?? 0,
    last.attachments?.length ?? 0,
  ].join(":");
}

function latestAssistantIsStreaming(messages: Message[]): boolean {
  const last = messages[messages.length - 1];
  return last?.role === "assistant" && last.status === "streaming";
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
    <div ref={sentinelRef} className="relative z-[1] flex justify-center pb-2">
      {error ? (
        <div
          role="alert"
          className={cn(
            "flex max-w-full items-center gap-2 rounded-md border px-2.5 py-1.5",
            "border-[var(--danger)]/25 bg-[var(--danger-soft)] text-xs text-[var(--fg-0)]",
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
          className="relative"
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
            onToggle={() => toggleCollapse(scene.anchorId)}
          />
          {!isCollapsed && (
            <div className="flex flex-col gap-3 pl-10 pr-3 pb-2">
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
      className="relative mx-auto w-full max-w-[1680px]"
    >
      {/* 贯穿竖线：margin-left 36px, 1px */}
      <div
        aria-hidden
        className="pointer-events-none absolute top-0 bottom-0 w-px bg-[var(--border-subtle)]"
        style={{ left: "24px" }}
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
    <div className="fixed left-1/2 bottom-[calc(96px+env(safe-area-inset-bottom,0px))] z-30 -translate-x-1/2">
      <Button
        size="sm"
        variant="secondary"
        leftIcon={<ArrowDownToLine className="h-3.5 w-3.5" aria-hidden />}
        onClick={onClick}
        className="border-white/15 bg-[var(--bg-1)]/88 shadow-lg backdrop-blur-xl"
      >
        最新
      </Button>
    </div>
  );
}

// ———————————————————————————————————————————————————
// 通用复制按钮：hover 时浮现，点击后显示"已复制"反馈
// ———————————————————————————————————————————————————
function CopyButton({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  };

  return (
    <button
      type="button"
      onClick={handleCopy}
      aria-label={copied ? "已复制" : "复制"}
      className={cn(
        "shrink-0 inline-flex items-center gap-1 h-6 rounded-md",
        "transition-all duration-150",
        copied
          ? "opacity-100 px-1.5 text-[var(--ok,#30A46C)] bg-[var(--ok,#30A46C)]/8"
          : cn(
              "opacity-0 group-hover/turn:opacity-60 hover:!opacity-100 px-1",
              "text-[var(--fg-3)] hover:text-[var(--fg-2)]",
            ),
        "active:scale-[0.9]",
        "focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
        className,
      )}
    >
      {copied ? (
        <>
          <Check className="w-3 h-3" />
          <span className="text-[10px] font-medium tracking-tight">已复制</span>
        </>
      ) : (
        <Copy className="w-3 h-3" />
      )}
    </button>
  );
}

// ———————————————————————————————————————————————————
// 用户 turn：右对齐，霞鹜文楷，左侧 2px × 40% 琥珀竖条
// ———————————————————————————————————————————————————
function UserTurn({ msg }: { msg: UserMessage }) {
  return (
    <div
      id={`msg-${msg.id}`}
      className="group/turn relative flex flex-col items-end gap-2"
    >
      <span
        aria-hidden
        className="absolute bg-[var(--amber-400)] shadow-[0_0_8px_var(--amber-glow)]"
        style={{
          left: "-24px",
          top: "30%",
          height: "40%",
          width: "2px",
          borderRadius: "1px",
        }}
      />

      {msg.attachments.length > 0 && (
        <div className="flex flex-wrap gap-2 justify-end">
          {msg.attachments.map((att, idx) => (
            <div
              key={att.id}
              className={cn(
                "relative w-12 h-12 rounded-lg overflow-hidden",
                "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
                idx === 0 && "ring-1 ring-[var(--amber-400)]/60",
              )}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={att.data_url}
                alt=""
                className="w-full h-full object-cover"
                loading="lazy"
              />
            </div>
          ))}
        </div>
      )}

      {msg.text && (
        <div className="flex items-start gap-2 max-w-[980px]">
          <CopyButton text={msg.text} className="mt-1" />
          <p
            className={cn(
              "text-right text-[15px] leading-[1.42] font-medium",
              "text-[var(--fg-0)] whitespace-pre-wrap break-words [overflow-wrap:anywhere]",
            )}
            style={{ fontFamily: "var(--font-zh-display)" }}
          >
            {msg.text}
          </p>
        </div>
      )}
    </div>
  );
}

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
  onOpenMenu: (info: ImageMenuInfo) => void;
}

function AssistantTurn({
  msg,
  generations,
  onRetryGen,
  onRetryText,
  onOpenMenu,
}: AssistantTurnProps) {
  const gens = generationIdsOf(msg)
    .map((id) => generations[id])
    .filter((g): g is Generation => Boolean(g));
  const isStreaming = msg.status === "streaming";
  const isChatLike =
    msg.intent_resolved === "chat" || msg.intent_resolved === "vision_qa";
  const isFailedText = msg.status === "failed" && isChatLike;
  const canCopy = Boolean(msg.text && msg.status !== "pending");

  return (
    <div id={`msg-${msg.id}`} className="group/turn flex flex-col gap-2">
      <CompletionStatusLine msg={msg} />

      {(msg.text || isFailedText) && (
        <div className="flex items-start gap-2">
          <div
            className={cn(
              "text-body-lg min-w-0 max-w-[1120px] break-words [overflow-wrap:anywhere] flex-1",
              "text-[var(--fg-0)]",
              "[&_pre]:max-w-full [&_pre]:overflow-x-auto [&_img]:max-w-full [&_img]:h-auto",
              isFailedText && "text-[var(--danger)]",
            )}
            style={{ fontFamily: "var(--font-body)" }}
          >
            {msg.text ? (
              <Markdown className="lumen-md-desktop-compact">{msg.text}</Markdown>
            ) : null}
            {isStreaming && (
              <span
                aria-hidden
                className="inline-block w-[0.5ch] ml-0.5 animate-pulse text-[var(--amber-400)]"
              >
                ▍
              </span>
            )}
          </div>
          {canCopy && (
            <CopyButton text={msg.text!} className="mt-0.5" />
          )}
        </div>
      )}

      {isFailedText && (
        <button
          type="button"
          onClick={() => onRetryText(msg.id)}
          className={cn(
            "self-start inline-flex items-center gap-1 px-2.5 h-7 rounded-full",
            "bg-[var(--bg-2)] border border-[var(--border)] text-[11px] text-[var(--fg-0)]",
            "hover:bg-[var(--bg-3)] transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          )}
          aria-label="重试"
        >
          <RotateCcw className="w-3 h-3" aria-hidden />
          重试
        </button>
      )}

      {gens.length > 0 && (
        <ImageGrid count={gens.length}>
          {gens.map((gen) => {
            if (
              gen.status === "queued" ||
              gen.status === "running" ||
              gen.status === "failed"
            ) {
              return (
                <DevelopingCard key={gen.id} gen={gen} onRetry={onRetryGen} />
              );
            }
            if (gen.status === "succeeded" && gen.image) {
              return (
                <FinalImage
                  key={gen.id}
                  gen={gen}
                  image={gen.image}
                  onOpenMenu={onOpenMenu}
                  inGrid={gens.length > 1}
                />
              );
            }
            return null;
          })}
        </ImageGrid>
      )}
    </div>
  );
}

function ImageGrid({ count, children }: { count: number; children: React.ReactNode }) {
  if (count === 1) {
    return <div className="flex w-full flex-col gap-2">{children}</div>;
  }

  const cols =
    count === 2 ? "grid-cols-2"
    : count === 3 ? "grid-cols-3"
    : count === 4 ? "grid-cols-2 md:grid-cols-4"
    : count === 5 ? "grid-cols-2 md:grid-cols-4 xl:grid-cols-5"
    : count === 6 ? "grid-cols-2 md:grid-cols-4 xl:grid-cols-6"
    : "grid-cols-2 md:grid-cols-4";

  return (
    <div className={cn("grid w-full min-w-0 gap-2", gridWidthClass(count), cols)}>
      {children}
    </div>
  );
}

// ———————————————————————————————————————————————————
// 最终图：hover 显示"···"；右键 / "···" → 打开菜单；单击 → Lightbox
// ———————————————————————————————————————————————————
interface FinalImageProps {
  gen: Generation;
  image: GeneratedImage;
  onOpenMenu: (info: ImageMenuInfo) => void;
  inGrid?: boolean;
}

const FinalImage = memo(function FinalImage({
  gen,
  image,
  onOpenMenu,
  inGrid = false,
}: FinalImageProps) {
  const [loaded, setLoaded] = useState(false);

  const ratioCss = aspectRatioToCss(gen.aspect_ratio);
  const ratio = aspectRatioNumber(image, gen.aspect_ratio);
  const isLongImage = ratio !== null && ratio < 0.58;
  const cardSrc = conversationImageSrc(image);
  const lightboxPreview =
    image.display_url ?? imageVariantUrl(image.id, "display2048");
  const elapsed = formatElapsed(gen);
  const tail = [
    gen.aspect_ratio,
    image.size_actual || `${image.width}x${image.height}`,
    elapsed ?? null,
  ]
    .filter(Boolean)
    .join(" · ");

  const handleCopy = () => {
    void navigator.clipboard
      ?.writeText(gen.prompt)
      .then(() => toast.success("已复制 prompt"))
      .catch(() => toast.error("复制失败"));
  };

  const handleClick = () => {
    const item: LightboxItem = {
      id: image.id,
      url: imageBinaryUrl(image.id),
      previewUrl: lightboxPreview,
      thumbUrl: lightboxThumbUrl(image),
      prompt: gen.prompt,
      width: image.width,
      height: image.height,
      aspect_ratio: gen.aspect_ratio,
      size_actual: image.size_actual || `${image.width}x${image.height}`,
      mime: image.mime,
    };
    openLightbox([item], image.id);
  };

  const handlePreviewIntent = () => {
    prewarmImage(lightboxPreview);
  };

  const openMenuAt = (x: number, y: number) => {
    onOpenMenu({
      imageId: image.id,
      prompt: gen.prompt,
      genId: gen.id,
      x,
      y,
    });
  };

  const handleContextMenu = (e: ReactMouseEvent<HTMLButtonElement>) => {
    e.preventDefault();
    openMenuAt(e.clientX, e.clientY);
  };

  const handleMoreClick = (e: ReactMouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    const rect = e.currentTarget.getBoundingClientRect();
    openMenuAt(rect.left, rect.bottom);
  };

  return (
    <div
      className={cn(
        "flex w-full flex-col gap-1 group",
        inGrid ? "justify-self-stretch" : singleImageWidthClass(ratio),
      )}
    >
      <div
        className={cn(
          "relative w-full overflow-hidden",
          "rounded-[var(--radius-md)] bg-[var(--bg-1)]",
          "border border-[var(--border-subtle)]/70 shadow-[var(--shadow-1)]",
          "transition-[border-color,opacity] duration-150 group-hover:border-[var(--fg-3)]/35",
          isLongImage && (inGrid ? "h-[min(20vh,180px)] min-h-[108px]" : "h-[min(24vh,220px)] min-h-[132px]"),
        )}
        style={
          isLongImage
            ? { contain: "layout paint" }
            : { aspectRatio: ratioCss, contain: "layout paint" }
        }
      >
        <button
          type="button"
          onClick={handleClick}
          onPointerEnter={handlePreviewIntent}
          onFocus={handlePreviewIntent}
          onContextMenu={handleContextMenu}
          aria-label="查看大图"
          className={cn(
            "absolute inset-0 block w-full h-full p-0 border-0 bg-transparent",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          )}
        >
          {/* skeleton 占位：图未 load 完之前给一层柔和 shimmer */}
          {!loaded && (
            <span
              aria-hidden
              className="absolute inset-0 bg-[var(--bg-2)] animate-pulse"
            />
          )}
          <ViewportImage
            src={cardSrc}
            alt={gen.prompt}
            rootMargin={inGrid ? "480px 0px" : "720px 0px"}
            persistAfterVisible
            fetchPriority="low"
            onLoad={() => setLoaded(true)}
            className={cn(
              "w-full h-full transition-opacity duration-300",
              isLongImage ? "object-contain" : "object-cover",
              loaded ? "opacity-100" : "opacity-0",
            )}
          />
          {isLongImage && (
            <>
              <span
                aria-hidden
                className="pointer-events-none absolute inset-x-0 bottom-0 h-20 bg-gradient-to-t from-black/70 to-transparent"
              />
              <span className="absolute bottom-3 left-3 rounded-full border border-white/15 bg-black/45 px-2.5 py-1 text-[11px] text-white/85 backdrop-blur">
                长图 · 点击查看完整
              </span>
            </>
          )}
        </button>

        {/* hover "···" 菜单按钮（兄弟节点，避免 button-in-button 嵌套） */}
        <button
          type="button"
          aria-label="更多操作"
          onClick={handleMoreClick}
          onContextMenu={handleContextMenu}
          className={cn(
            "absolute top-1.5 right-1.5 inline-flex items-center justify-center",
            "w-7 h-7 rounded-full",
            "bg-[rgba(8,8,10,0.65)] backdrop-blur-sm",
            "border border-[var(--border-subtle)] text-[var(--fg-0)]",
            "opacity-0 group-hover:opacity-100 transition-opacity",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 focus-visible:opacity-100",
          )}
        >
          <MoreHorizontal className="w-4 h-4" aria-hidden />
        </button>
      </div>

      <button
        type="button"
        onClick={handleCopy}
        className={cn(
          "group/meta self-start inline-flex items-center gap-1",
          "text-left px-1 py-px rounded text-[11px] tabular-nums text-[var(--fg-3)]",
          "hover:text-[var(--fg-2)] transition-colors duration-150",
        )}
        style={{ fontFamily: "var(--font-mono)" }}
        aria-label="复制 prompt"
        title={gen.prompt}
      >
        <span>{tail}</span>
        <Copy className="w-3 h-3 opacity-0 group-hover/meta:opacity-100 transition-opacity shrink-0" />
      </button>
    </div>
  );
});

// ———————————————————————————————————————————————————
// 桌面右键 / "···" 上下文菜单：absolute portal 到 body，点外 / ESC 关闭
// ———————————————————————————————————————————————————
interface ImageContextMenuProps {
  info: ImageMenuInfo | null;
  onClose: () => void;
  onEditImage: (imageId: string) => void;
  onRetryGen: (genId: string) => void;
  onLocate: (imageId: string) => void;
}

function ImageContextMenu({
  info,
  onClose,
  onEditImage,
  onRetryGen,
  onLocate,
}: ImageContextMenuProps) {
  if (!info) return null;
  return (
    <ImageContextMenuInner
      info={info}
      onClose={onClose}
      onEditImage={onEditImage}
      onRetryGen={onRetryGen}
      onLocate={onLocate}
    />
  );
}

interface ImageContextMenuInnerProps {
  info: ImageMenuInfo;
  onClose: () => void;
  onEditImage: (imageId: string) => void;
  onRetryGen: (genId: string) => void;
  onLocate: (imageId: string) => void;
}

function ImageContextMenuInner({
  info,
  onClose,
  onEditImage,
  onRetryGen,
  onLocate,
}: ImageContextMenuInnerProps) {
  const menuRef = useRef<HTMLDivElement | null>(null);

  // 首帧 React 按"用户光标点"渲染；layout 完成后由 effect 直接改写 DOM style，
  // 把菜单夹到视口内。避免在 effect 中 setState 触发级联渲染（React 19 hooks 规则）。
  useEffect(() => {
    const el = menuRef.current;
    if (!el) return;
    const { offsetWidth, offsetHeight } = el;
    const vw = typeof window !== "undefined" ? window.innerWidth : 1024;
    const vh = typeof window !== "undefined" ? window.innerHeight : 768;
    const padding = 8;
    const left = Math.min(
      Math.max(padding, info.x),
      Math.max(padding, vw - offsetWidth - padding),
    );
    const top = Math.min(
      Math.max(padding, info.y),
      Math.max(padding, vh - offsetHeight - padding),
    );
    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
  }, [info.x, info.y]);

  // 点外 / ESC 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onDown = (e: MouseEvent) => {
      const el = menuRef.current;
      if (el && e.target instanceof Node && !el.contains(e.target)) {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    // capture：防止页内其它 mousedown 把菜单自己吃掉
    window.addEventListener("mousedown", onDown, true);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown, true);
    };
  }, [onClose]);

  if (typeof document === "undefined") return null;

  const actions: Array<{ key: string; label: string; icon: ReactNode; onSelect: () => void }> = [
    {
      key: "ref",
      label: "做参考图",
      icon: <ImagePlus className="w-4 h-4" />,
      onSelect: () => onEditImage(info.imageId),
    },
    {
      key: "save",
      label: "保存",
      icon: <Download className="w-4 h-4" />,
      onSelect: () => {
        const url = imageVariantUrl(info.imageId, "preview1024");
        window.open(url, "_blank", "noopener,noreferrer");
      },
    },
    {
      key: "copy",
      label: "复制 prompt",
      icon: <Clipboard className="w-4 h-4" />,
      onSelect: () => {
        void navigator.clipboard
          ?.writeText(info.prompt)
          .then(() => toast.success("已复制 prompt"))
          .catch(() => toast.error("复制失败"));
      },
    },
    {
      key: "regen",
      label: "再生一张",
      icon: <RefreshCw className="w-4 h-4" />,
      onSelect: () => onRetryGen(info.genId),
    },
    {
      key: "locate",
      label: "在图库定位",
      icon: <ExternalLink className="w-4 h-4" />,
      onSelect: () => onLocate(info.imageId),
    },
  ];

  const style: React.CSSProperties = {
    top: info.y,
    left: info.x,
  };

  return createPortal(
    <div
      ref={menuRef}
      role="menu"
      aria-label="图片操作"
      className={cn(
        "fixed z-[1000] min-w-[172px] py-1",
        "rounded-xl border border-[var(--border)]",
        "bg-[var(--bg-1)]/90 backdrop-blur-xl shadow-[var(--shadow-3)]",
      )}
      style={style}
    >
      {actions.map((a) => (
        <button
          key={a.key}
          type="button"
          role="menuitem"
          onClick={() => {
            a.onSelect();
            onClose();
          }}
          className={cn(
            "w-full text-left px-3 h-8 flex items-center gap-2",
            "text-[13px] text-[var(--fg-0)]",
            "hover:bg-[var(--bg-2)] transition-colors duration-100",
            "focus-visible:outline-none focus-visible:bg-[var(--bg-2)]",
          )}
        >
          <span className="text-[var(--fg-2)] shrink-0">{a.icon}</span>
          {a.label}
        </button>
      ))}
    </div>,
    document.body,
  );
}
