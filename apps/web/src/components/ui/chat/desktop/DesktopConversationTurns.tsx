"use client";

// 桌面端创作画布：单一内容轴 + Scene 分隔 + DevelopingCard 显影扫光。
// 按 messages 顺序两两配对（user → assistant），渲染 Scene NN 分隔条。
// 跟移动端 MobileConversationCanvas 设计哲学一致，差异：
//   - 桌面端提示词、文本和单图统一到 760px 内容轴
//   - 单图按视口高度限制，优先完整显示
//   - 右键 / hover"···" 触发上下文菜单（移动端长按）
//   - 保留虚拟化（messages > 80）

import {
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
  memo,
  useState,
} from "react";
import {
  Check,
  Copy,
  RotateCcw,
} from "lucide-react";
import { Button, IconButton, toast } from "@/components/ui/primitives";
import { Markdown } from "@/components/ui/Markdown";
import { useUiStore } from "@/store/useUiStore";
import { cn } from "@/lib/utils";
import { tryCopyTextToClipboard } from "@/lib/clipboard";
import { CompletionStatusLine } from "@/components/ui/chat/CompletionStatusLine";
import {
  ConversationTurn,
  ConversationUserTurn,
  FinalImage,
  lightboxItemForConversationImage,
  type ImageMenuInfo,
} from "@/components/ui/chat/ConversationVisualAtoms";
import type {
  AssistantMessage,
  Generation,
  GeneratedImage,
  Intent,
  Message,
  UserMessage,
} from "@/lib/types";
import type { LightboxItem } from "@/components/ui/lightbox/types";
import { DevelopingCard } from "@/components/ui/chat/mobile";
import { generationRenderSignature } from "@/components/ui/chat/generationRenderSignature";

function generationIdsOf(msg: AssistantMessage): string[] {
  if (msg.generation_ids?.length) return msg.generation_ids;
  return msg.generation_id ? [msg.generation_id] : [];
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
    <IconButton
      size="sm"
      onClick={handleCopy}
      aria-label={copied ? "已复制" : "复制"}
      tooltip={copied ? "已复制" : "复制"}
      className={cn(
        "transition-all duration-150",
        copied
          ? "bg-success-soft text-success opacity-100"
          : "text-[var(--fg-3)] opacity-0 group-hover/turn:opacity-60 hover:!opacity-100",
        "focus-visible:opacity-100",
        className,
      )}
    >
      {copied ? (
        <Check className="h-3.5 w-3.5" aria-hidden />
      ) : (
        <Copy className="h-3.5 w-3.5" aria-hidden />
      )}
    </IconButton>
  );
}

// ———————————————————————————————————————————————————
// 用户 turn：右对齐的轻量 raised surface。
// ———————————————————————————————————————————————————

const CONVERSATION_TEXT_RAIL =
  "mx-auto w-full max-w-[var(--content-composer)]";

export const UserTurn = memo(function UserTurn({ msg }: { msg: UserMessage }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    if (!msg.text) return;
    void tryCopyTextToClipboard(msg.text).then((success) => {
      if (!success) {
        toast.error("复制失败");
        return;
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  };

  return (
    <ConversationUserTurn
      msg={msg}
      copied={copied}
      onCopy={handleCopy}
    />
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
    <ConversationTurn
      id={`msg-${msg.id}`}
      side="assistant"
      className="flex flex-col gap-2"
    >
      <div className={CONVERSATION_TEXT_RAIL}>
        <CompletionStatusLine msg={msg} />
      </div>

      {(msg.text || isFailedText) && (
        <div className={cn(CONVERSATION_TEXT_RAIL, "flex items-start gap-2")}>
          <div
            className={cn(
              "type-body min-w-0 max-w-[var(--content-text)] break-words [overflow-wrap:anywhere] flex-1",
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
                className="ml-0.5 inline-block w-[0.5ch] animate-pulse text-accent"
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
          <Button
            size="sm"
            variant="secondary"
            onClick={() => onRetryText(msg.id)}
            className="h-8 px-2.5"
            aria-label="重试"
            leftIcon={<RotateCcw className="h-3.5 w-3.5" aria-hidden />}
          >
            重试
          </Button>
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
                <DesktopFinalImage
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

interface DesktopFinalImageProps {
  gen: Generation;
  image: GeneratedImage;
  onOpenMenu: (info: ImageMenuInfo) => void;
  inGrid?: boolean;
}

const DesktopFinalImage = memo(function DesktopFinalImage({
  gen,
  image,
  onOpenMenu,
  inGrid = false,
}: DesktopFinalImageProps) {
  const handleCopy = () => {
    void tryCopyTextToClipboard(gen.prompt).then((success) => {
      if (success) toast.success("已复制 prompt");
      else toast.error("复制失败");
    });
  };

  const handleClick = () => {
    const item = lightboxItemForConversationImage(gen, image);
    openLightbox([item], image.id);
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
    <FinalImage
      gen={gen}
      image={image}
      platform="desktop"
      inGrid={inGrid}
      onPreview={handleClick}
      onCopy={handleCopy}
      onContextMenu={handleContextMenu}
      onOpenMenu={handleMoreClick}
    />
  );
});

// ———————————————————————————————————————————————————
// 桌面右键 / "···" 上下文菜单：absolute portal 到 body，点外 / ESC 关闭
// ———————————————————————————————————————————————————
