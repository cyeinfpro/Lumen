"use client";

// Darkroom 移动端画布：无气泡 + Scene 胶片竖线（距左 12px）。
// 按 messages 顺序两两配对（user → assistant），渲染 Scene NN 分隔条。

import {
  type RefObject,
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AlertTriangle,
  ArrowDownToLine,
  Copy,
  Check,
  ImagePlus,
  RotateCcw,
} from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { Markdown } from "@/components/ui/Markdown";
import { ViewportImage } from "@/components/ui/ViewportImage";
import { cn } from "@/lib/utils";
import { CompletionStatusLine } from "@/components/ui/chat/CompletionStatusLine";
import { useHistoryPaging } from "@/hooks/useHistoryPaging";

import type {
  AssistantMessage,
  Generation,
  GeneratedImage,
  Intent,
  Message,
  UserMessage,
} from "@/lib/types";
import { cancelTask, imageBinaryUrl, imageVariantUrl } from "@/lib/apiClient";
import { prewarmImage } from "@/lib/imagePreload";
import { aspectRatioToCss } from "@/lib/sizing";
import type { LightboxItem } from "@/components/ui/lightbox/types";
import { DevelopingCard } from "./DevelopingCard";
import { SceneDivider } from "./SceneDivider";

interface MobileConversationCanvasProps {
  messages: Message[];
  generations: Record<string, Generation>;
  scrollRef?: RefObject<HTMLDivElement | null>;
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
  if (ratio !== null && ratio < 0.58) return "max-w-[min(44%,176px)]";
  if (ratio !== null && ratio < 0.9) return "max-w-[min(60%,260px)]";
  if (ratio !== null && ratio > 1.7) return "max-w-[min(82%,340px)]";
  return "max-w-[min(76%,320px)]";
}

function openLightbox(
  items: LightboxItem[],
  initialId: string,
  fromRect: DOMRect | null,
) {
  if (typeof window === "undefined") return;
  if (items.length === 0) return;
  window.dispatchEvent(
    new CustomEvent("lumen:open-lightbox", {
      // MobileLightbox 监听契约：{ items: LightboxItem[], initialId, fromRect? }
      detail: { items, initialId, fromRect: fromRect ?? undefined },
    }),
  );
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
    <div ref={sentinelRef} className="relative z-[1] flex justify-center pb-1.5">
      {error ? (
        <div
          role="alert"
          className={cn(
            "flex max-w-full items-center gap-2 rounded-md border px-2.5 py-2",
            "border-[var(--danger)]/25 bg-[var(--danger-soft)] text-xs text-[var(--fg-0)]",
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
            className="h-7 shrink-0 px-2 text-xs"
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
          className="h-7 text-xs text-[var(--fg-2)]"
        >
          {loading ? "正在加载" : "加载更早消息"}
        </Button>
      )}
    </div>
  );
}

export function MobileConversationCanvas({
  messages,
  generations,
  scrollRef,
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

      <div className="flex flex-col">
        {scenes.map((scene) => {
          const isCollapsed = collapsed.has(scene.anchorId);
          return (
            <section
              key={scene.anchorId}
              id={`scene-${scene.anchorId}`}
              data-history-scroll-anchor={scene.anchorId}
              aria-label={`Scene ${String(scene.index).padStart(2, "0")}`}
              className="relative"
              style={{
                contentVisibility: "auto",
                containIntrinsicSize: "320px",
              }}
            >
              <SceneDivider
                index={scene.index}
                collapsed={isCollapsed}
                onToggle={() => toggleCollapse(scene.anchorId)}
              />
              {!isCollapsed && (
                <div className="flex flex-col gap-2.5 pl-7 pr-0.5 pb-1.5">
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
        })}
      </div>

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
    <div className="fixed left-1/2 bottom-[calc(112px+env(safe-area-inset-bottom,0px))] z-30 -translate-x-1/2">
      <Button
        size="sm"
        variant="secondary"
        leftIcon={<ArrowDownToLine className="h-3.5 w-3.5" aria-hidden />}
        onClick={onClick}
        className="h-8 border-white/15 bg-[var(--bg-1)]/90 px-3 text-xs shadow-lg backdrop-blur-xl"
      >
        最新
      </Button>
    </div>
  );
}

// ———————————————————————————————————————————————————
// 用户 turn：右对齐，霞鹜文楷，左侧 2px × 40% 琥珀竖条
// ———————————————————————————————————————————————————
function UserTurn({ msg }: { msg: UserMessage }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    if (!msg.text) return;
    void navigator.clipboard?.writeText(msg.text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    });
  };

  return (
    <div id={`msg-${msg.id}`} className="relative flex flex-col items-end gap-2">
      <span
        aria-hidden
        className="absolute bg-[var(--amber-400)] shadow-[0_0_8px_var(--amber-glow)]"
        style={{
          left: "-15px",
          top: "25%",
          height: "50%",
          width: "2px",
          borderRadius: "1px",
        }}
      />

      {msg.attachments.length > 0 && (
        <div className="flex flex-wrap gap-1.5 justify-end">
          {msg.attachments.map((att, idx) => (
            <div
              key={att.id}
              className={cn(
                "relative w-11 h-11 rounded-lg overflow-hidden",
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
        <div className="flex items-start gap-2 w-full">
          <p
            className={cn(
              "text-right text-[15px] font-medium leading-[1.55] flex-1 min-w-0",
              "text-[var(--fg-0)] whitespace-pre-wrap break-words [overflow-wrap:anywhere]",
            )}
            style={{ fontFamily: "var(--font-zh-display)" }}
          >
            {msg.text}
          </p>
          <button
            type="button"
            onClick={copy}
            aria-label="复制"
            className="mt-1 p-1.5 rounded-lg text-[var(--fg-3)] hover:text-[var(--fg-1)] active:scale-[0.92] active:bg-[var(--bg-2)] transition-all shrink-0"
          >
            {copied ? <Check className="w-3.5 h-3.5 text-[var(--ok,#30A46C)]" /> : <Copy className="w-3.5 h-3.5" />}
          </button>
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
}

function AssistantTurn({
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
    void navigator.clipboard?.writeText(msg.text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    });
  };

  const gens = generationIdsOf(msg)
    .map((id) => generations[id])
    .filter((g): g is Generation => Boolean(g));
  const isStreaming = msg.status === "streaming";
  const isChatLike =
    msg.intent_resolved === "chat" || msg.intent_resolved === "vision_qa";
  const isFailedText = msg.status === "failed" && isChatLike;
  const canCopy = Boolean(msg.text && msg.status !== "pending");

  return (
    <div id={`msg-${msg.id}`} className="flex flex-col gap-2">
      <CompletionStatusLine msg={msg} compact />

      {/* 助手正文 */}
      {(msg.text || isFailedText) && (
        <div className="flex items-start gap-2">
          <div
            className={cn(
              "text-[15px] leading-[1.55] min-w-0 break-words [overflow-wrap:anywhere] flex-1",
              "text-[var(--fg-0)]",
              "[&_pre]:max-w-full [&_pre]:overflow-x-auto [&_img]:max-w-full [&_img]:h-auto",
              isFailedText && "text-[var(--danger)]",
            )}
            style={{ fontFamily: "var(--font-body)" }}
          >
            {msg.text ? <Markdown>{msg.text}</Markdown> : null}
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
            <button
              type="button"
              onClick={copy}
              aria-label="复制"
              className="mt-1 p-1.5 rounded-lg text-[var(--fg-3)] hover:text-[var(--fg-1)] active:scale-[0.92] active:bg-[var(--bg-2)] transition-all shrink-0"
            >
              {copied ? <Check className="w-3.5 h-3.5 text-[var(--ok,#30A46C)]" /> : <Copy className="w-3.5 h-3.5" />}
            </button>
          )}
        </div>
      )}

      {/* 文本失败重试 */}
      {isFailedText && (
        <button
          type="button"
          onClick={() => onRetryText(msg.id)}
          className={cn(
            "self-start inline-flex items-center gap-1 px-3 h-7 rounded-full",
            "bg-[var(--bg-2)] border border-[var(--border)] text-[12px] text-[var(--fg-0)]",
            "active:scale-[0.97] transition-transform",
          )}
          aria-label="重试"
        >
          <RotateCcw className="w-3.5 h-3.5" aria-hidden />
          重试
        </button>
      )}

      {/* 已完成的助手消息：提供重新生成按钮 */}
      {msg.status === "succeeded" && gens.length > 0 && gens.every((g) => g.status === "succeeded") && (
        <div className="flex items-center gap-2 pt-0.5">
          <button
            type="button"
            onClick={() => onRegenerate(msg.id, "text_to_image")}
            className={cn(
              "inline-flex items-center gap-1 px-3 h-7 rounded-full",
              "border border-[var(--border-subtle)] bg-[var(--bg-1)]",
              "text-[12px] text-[var(--fg-2)]",
              "active:scale-[0.97] active:bg-[var(--bg-2)] transition-all",
            )}
          >
            <RotateCcw className="w-3 h-3" aria-hidden />
            重新生成
          </button>
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
    </div>
  );
}

// ———————————————————————————————————————————————————
// 最终图 + 参数尾行 + 点击 Lightbox
// ———————————————————————————————————————————————————
interface FinalImageProps {
  gen: Generation;
  image: GeneratedImage;
  onEditImage: (id: string) => void;
  inGrid?: boolean;
}

const FinalImage = memo(function FinalImage({
  gen,
  image,
  onEditImage,
  inGrid = false,
}: FinalImageProps) {
  const imgRef = useRef<HTMLImageElement | null>(null);
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
      .then(() => pushMobileToast("已复制 prompt", "success"))
      .catch(() => pushMobileToast("复制失败", "danger"));
  };

  const handleClick = () => {
    // 点击图：打开 Lightbox（Phase 6 监听该事件）
    const rect = imgRef.current?.getBoundingClientRect() ?? null;
    const item: LightboxItem = {
      id: image.id,
      // url 用 binary 保留下载 / 新标签页打开原图能力；
      // previewUrl 用 display2048 避免手机 decode 4K 原图卡死。
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
    openLightbox([item], image.id, rect);
  };

  const handlePreviewIntent = () => {
    prewarmImage(lightboxPreview);
  };

  return (
    <div
      className={cn("flex w-full flex-col gap-1", inGrid ? "" : singleImageWidthClass(ratio))}
    >
      <button
        type="button"
        onClick={handleClick}
        onPointerDown={handlePreviewIntent}
        onFocus={handlePreviewIntent}
        aria-label="查看大图"
        className={cn(
          "relative block w-full overflow-hidden p-0",
          "rounded-[var(--radius-md)] bg-[var(--bg-1)]",
          "shadow-[var(--shadow-1)]",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          isLongImage && (inGrid ? "h-[min(24vh,168px)] min-h-[112px]" : "h-[min(30vh,220px)] min-h-[140px]"),
        )}
        style={
          isLongImage
            ? { contain: "layout paint" }
            : { aspectRatio: ratioCss, contain: "layout paint" }
        }
      >
        {/* skeleton 占位：图未 load 完之前给一层柔和 shimmer */}
        {!loaded && (
          <span
            aria-hidden
            className="absolute inset-0 bg-[var(--bg-2)] animate-pulse"
          />
        )}
        <ViewportImage
          ref={imgRef}
          src={cardSrc}
          alt={gen.prompt}
          rootMargin={inGrid ? "320px 0px" : "520px 0px"}
          persistAfterVisible
          fetchPriority="low"
          onLoad={() => setLoaded(true)}
          className={cn(
            "w-full h-full transition-opacity duration-300",
            isLongImage ? "object-contain" : "object-cover",
            loaded ? "opacity-100" : "opacity-0",
          )}
        />
      </button>
      <div className="flex items-center gap-1.5 px-0.5">
        <button
          type="button"
          onClick={handleCopy}
          className={cn(
            "text-left text-[10px] tabular-nums text-[var(--fg-3)] truncate flex-1 min-w-0",
            "hover:text-[var(--fg-1)] transition-colors active:opacity-70",
          )}
          style={{ fontFamily: "var(--font-mono)" }}
          aria-label="复制 prompt"
          title={gen.prompt}
        >
          {tail}
        </button>
        <button
          type="button"
          onClick={() => onEditImage(image.id)}
          className={cn(
            "shrink-0 inline-flex items-center gap-1 h-6 px-2 rounded-full",
            "border border-[var(--border-subtle)] bg-[var(--bg-2)]",
            "text-[10px] text-[var(--fg-2)] hover:text-[var(--fg-0)]",
            "active:scale-[0.95] transition-all",
          )}
          aria-label="用作参考图"
        >
          <ImagePlus className="w-3 h-3" aria-hidden />
          参考图
        </button>
      </div>
    </div>
  );
});
