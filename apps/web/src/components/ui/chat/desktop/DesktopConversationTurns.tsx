"use client";

// 桌面端创作画布：单一内容轴 + Scene 分隔 + DevelopingCard 显影扫光。
// 按 messages 顺序两两配对（user → assistant），渲染 Scene NN 分隔条。
// 跟移动端 MobileConversationCanvas 设计哲学一致，差异：
//   - 桌面端提示词、文本和单图统一到 760px 内容轴
//   - 单图按视口高度限制，优先完整显示
//   - 右键 / hover"···" 触发上下文菜单（移动端长按）
//   - 保留虚拟化（messages > 80）

import {
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  memo,
  useState,
} from "react";
import {
  Check,
  Copy,
  MoreHorizontal,
  RotateCcw,
} from "lucide-react";
import { toast } from "@/components/ui/primitives";
import { Markdown } from "@/components/ui/Markdown";
import { ViewportImage } from "@/components/ui/ViewportImage";
import { useUiStore } from "@/store/useUiStore";
import { cn } from "@/lib/utils";
import { tryCopyTextToClipboard } from "@/lib/clipboard";
import { CompletionStatusLine } from "@/components/ui/chat/CompletionStatusLine";
import type {
  AssistantMessage,
  Generation,
  GeneratedImage,
  Intent,
  Message,
  UserMessage,
} from "@/lib/types";
import { imageVariantUrl } from "@/lib/apiClient";
import { prewarmImage } from "@/features/assets";
import { aspectRatioToCss } from "@/lib/sizing";
import { imageResultToLightboxItem } from "@/lib/imageResultLightbox";
import type { LightboxItem } from "@/components/ui/lightbox/types";
import { DevelopingCard } from "@/components/ui/chat/mobile";
import { generationRenderSignature } from "@/components/ui/chat/generationRenderSignature";

interface ImageMenuInfo {
  imageId: string;
  prompt: string;
  genId: string;
  x: number;
  y: number;
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

function singleImageFrameStyle(ratio: number | null): CSSProperties {
  if (ratio === null) {
    return { width: "min(100%, 620px)" };
  }

  const maxWidth =
    ratio < 0.75 ? 480
    : ratio <= 1.2 ? 580
    : ratio <= 1.8 ? 780
    : 860;
  const viewportHeightWidth = `${(ratio * 58).toFixed(2)}dvh`;
  return {
    width: `min(100%, ${maxWidth}px, ${viewportHeightWidth})`,
  };
}

function gridWidthClass(count: number): string {
  if (count === 2) return "max-w-[720px]";
  if (count === 3 || count === 4) return "max-w-[960px]";
  if (count >= 5) return "max-w-[1080px]";
  return "max-w-[960px]";
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

function isFreeGeneration(gen: Generation, image: GeneratedImage): boolean {
  return (
    gen.billing_free === true ||
    gen.billing_label === "free" ||
    gen.is_dual_race_bonus === true ||
    image.billing_free === true ||
    image.billing_label === "free" ||
    image.is_dual_race_bonus === true
  );
}

export function generationSignature(generations: Record<string, Generation>): string {
  return Object.values(generations)
    .map((g) => `${g.id}:${g.status}:${g.stage}:${g.image?.id ?? ""}`)
    .join("|");
}

export function messageScrollSignature(messages: Message[]): string {
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

export function latestAssistantIsStreaming(messages: Message[]): boolean {
  const last = messages[messages.length - 1];
  return last?.role === "assistant" && last.status === "streaming";
}

function assistantGenerationsRenderSignature(
  msg: AssistantMessage,
  generations: Record<string, Generation>,
): string {
  return generationIdsOf(msg)
    .map((id) => generationRenderSignature(generations[id]))
    .join("|");
}

function CopyButton({
  text,
  className,
}: {
  text: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    void tryCopyTextToClipboard(text).then((success) => {
      if (!success) {
        toast.error("复制失败");
        return;
      }
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
        "shrink-0 inline-flex items-center gap-1 h-6 rounded-[var(--radius-control)]",
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
// 用户 turn：右对齐的轻量 raised surface。
// ———————————————————————————————————————————————————

const CONVERSATION_TEXT_RAIL =
  "mx-auto w-full max-w-[var(--content-composer)]";

export const UserTurn = memo(function UserTurn({ msg }: { msg: UserMessage }) {
  return (
    <div
      id={`msg-${msg.id}`}
      className={cn("group/turn relative", CONVERSATION_TEXT_RAIL)}
    >
      <div className="flex items-start gap-3 border-l border-[var(--accent-border)] pl-4">
        <div className="min-w-0 flex-1">
          {msg.attachments.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {msg.attachments.map((att) => (
                <div
                  key={att.id}
                  className="relative h-11 w-11 overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-2)]"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={att.data_url}
                    alt=""
                    className="h-full w-full object-cover"
                    loading="lazy"
                  />
                </div>
              ))}
            </div>
          )}

          {msg.text && (
            <div className="flex items-start gap-2">
              <p
                className={cn(
                  "min-w-0 flex-1 whitespace-pre-wrap break-words text-left text-[14px] font-normal leading-[1.65]",
                  "text-[var(--fg-0)] [overflow-wrap:anywhere]",
                )}
                style={{ fontFamily: "var(--font-zh-body)" }}
              >
                {msg.text}
              </p>
              <CopyButton text={msg.text} className="mt-0.5" />
            </div>
          )}
        </div>
      </div>
    </div>
  );
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
  onOpenMenu: (info: ImageMenuInfo) => void;
}

export const AssistantTurn = memo(function AssistantTurn({
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
      <div className={CONVERSATION_TEXT_RAIL}>
        <CompletionStatusLine msg={msg} />
      </div>

      {(msg.text || isFailedText) && (
        <div className={cn(CONVERSATION_TEXT_RAIL, "flex items-start gap-2")}>
          <div
            className={cn(
              "text-body-lg min-w-0 max-w-[var(--content-text)] break-words [overflow-wrap:anywhere] flex-1",
              "text-[var(--fg-0)]",
              "[&_pre]:max-w-full [&_pre]:overflow-x-auto [&_img]:max-w-full [&_img]:h-auto",
              isFailedText && "text-[var(--danger)]",
            )}
            style={{ fontFamily: "var(--font-body)" }}
          >
            {msg.text ? (
              <Markdown
                className="lumen-md-desktop-compact"
                autoDetectCode={!isStreaming}
              >
                {msg.text}
              </Markdown>
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
        <div className={CONVERSATION_TEXT_RAIL}>
          <button
            type="button"
            onClick={() => onRetryText(msg.id)}
            className={cn(
              "inline-flex h-7 items-center gap-1 rounded-full px-2.5",
              "bg-[var(--bg-2)] border border-[var(--border)] text-[11px] text-[var(--fg-0)]",
              "hover:bg-[var(--bg-3)] transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
            )}
            aria-label="重试"
          >
            <RotateCcw className="w-3 h-3" aria-hidden />
            重试
          </button>
        </div>
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
    prev.onRegenerate !== next.onRegenerate ||
    prev.onOpenMenu !== next.onOpenMenu
  ) {
    return false;
  }
  return (
    assistantGenerationsRenderSignature(prev.msg, prev.generations) ===
    assistantGenerationsRenderSignature(next.msg, next.generations)
  );
}

export function ImageGrid({ count, children }: { count: number; children: ReactNode }) {
  if (count === 1) {
    return <div className="flex w-full flex-col items-center gap-2">{children}</div>;
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

export const FinalImage = memo(function FinalImage({
  gen,
  image,
  onOpenMenu,
  inGrid = false,
}: FinalImageProps) {
  const [loaded, setLoaded] = useState(false);

  const ratioCss = aspectRatioToCss(gen.aspect_ratio);
  const ratio = aspectRatioNumber(image, gen.aspect_ratio);
  const cardSrc = conversationImageSrc(image);
  const lightboxPreview =
    image.display_url ?? imageVariantUrl(image.id, "display2048");
  const free = isFreeGeneration(gen, image);
  const elapsed = formatElapsed(gen);
  const tail = [
    gen.aspect_ratio,
    image.size_actual || `${image.width}x${image.height}`,
    elapsed ?? null,
  ]
    .filter(Boolean)
    .join(" · ");

  const handleCopy = () => {
    void tryCopyTextToClipboard(gen.prompt).then((success) => {
      if (success) toast.success("已复制 prompt");
      else toast.error("复制失败");
    });
  };

  const handleClick = () => {
    const item = imageResultToLightboxItem(gen, image, {
      previewUrl: lightboxPreview,
      thumbUrl: lightboxThumbUrl(image),
      createdAt: gen.finished_at ?? gen.started_at,
    });
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
        inGrid ? "justify-self-stretch" : "mx-auto",
      )}
      style={inGrid ? undefined : singleImageFrameStyle(ratio)}
    >
      <div
        className={cn(
          "relative w-full overflow-hidden",
          "rounded-[var(--radius-md)] bg-[var(--bg-1)]",
          "border border-[var(--border-subtle)]/70 shadow-[var(--shadow-1)]",
          "transition-[border-color,opacity] duration-150 group-hover:border-[var(--fg-3)]/35",
        )}
        style={{ aspectRatio: ratioCss, contain: "layout paint" }}
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
              "object-contain",
              loaded ? "opacity-100" : "opacity-0",
            )}
          />
        </button>

        {free && (
          <span className="pointer-events-none absolute left-2 top-2 z-10 rounded-full border border-[var(--border-strong)] bg-black/60 px-2 py-0.5 font-mono text-[10px] tracking-[0.14em] text-white backdrop-blur">
            free
          </span>
        )}

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
