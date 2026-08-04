"use client";

/* eslint-disable @next/next/no-img-element -- Reference previews are authenticated API media URLs. */

import { useEffect, useRef, useState } from "react";
import {
  AudioLines,
  ImageIcon,
  PencilLine,
  RefreshCw,
  Sparkles,
  Tags,
  Video as VideoIcon,
  X,
  XCircle,
} from "lucide-react";

import { Button, IconButton, toast } from "@/components/ui/primitives";
import { videoBinaryUrl } from "@/lib/apiClient";
import type { VideoReferenceMediaIn } from "@/lib/types";
import { cn } from "@/lib/utils";
import type { ReferenceDraft } from "@/lib/video/types";

import {
  focusWorkbenchElement,
  isTopmostDialog,
  restoreWorkbenchFocus,
  trapDialogFocus,
} from "./video-dialog-focus";
import {
  PromptEnhanceCandidateCardView,
  PromptEnhanceCandidatePreviewView,
  PromptEnhanceLoadingStateView,
} from "./video-prompt-enhance-candidate-ui";
import { ReferenceThumbnailView } from "./video-reference-thumbnail";
import {
  ModeCard,
  VideoParameterPanelView,
  VideoWorkbenchHeader,
} from "./video-workbench-controls";
import type { VideoParameterPanelProps } from "./video-workbench-controls";

export type { ReferenceDraft } from "@/lib/video/types";
export { ModeCard, VideoWorkbenchHeader };

export type PromptEnhanceAction =
  | "direct_pass"
  | "light_refine"
  | "direct_rewrite"
  | "ask_first"
  | "keep_original"
  | "optional_vc";

export type PromptEnhanceCandidate = {
  id: string;
  title: string;
  prompt: string;
  action: PromptEnhanceAction;
};

const REFERENCE_REF_ID_RE = /^ref:(image|video|audio):([1-9][0-9]{0,2})$/;

export function isTopmostVideoDialog(dialog: HTMLElement | null): boolean {
  return isTopmostDialog(dialog);
}

export function focusVideoWorkbenchElement(
  target: HTMLElement | null,
  options?: FocusOptions,
  blocked = false,
): boolean {
  return focusWorkbenchElement(target, options, blocked);
}

export function restoreVideoWorkbenchFocus(
  previousFocus: HTMLElement | null,
  closingDialog: HTMLElement | null,
): void {
  restoreWorkbenchFocus(previousFocus, closingDialog, (target) => {
    target.focus({ preventScroll: true });
  });
}

export function trapVideoDialogFocus(
  event: KeyboardEvent,
  dialog: HTMLElement | null,
): void {
  trapDialogFocus(event, dialog);
}

export function cleanPromptEnhanceText(value: string): string {
  return value
    .replace(/\r\n/g, "\n")
    .replace(/^```(?:json|text)?\s*/i, "")
    .replace(/\s*```$/i, "")
    .replace(/^(?:提示词|prompt)\s*[:：]\s*/i, "")
    .trim()
    .replace(/^["“]|["”]$/g, "")
    .trim();
}

export function shouldAutoApplyPromptEnhanceCandidate(
  candidate: PromptEnhanceCandidate,
): boolean {
  return (
    candidate.action === "direct_pass" || candidate.action === "light_refine"
  );
}

export function canApplyPromptEnhanceCandidate(
  candidate: PromptEnhanceCandidate,
): boolean {
  return (
    candidate.action !== "ask_first" && candidate.action !== "keep_original"
  );
}

function promptEnhanceCandidateButtonText(
  candidate: PromptEnhanceCandidate,
  selected: boolean,
): string {
  if (!canApplyPromptEnhanceCandidate(candidate)) return "仅查看";
  if (selected) return "已应用";
  return "应用此版本";
}

function cleanReferencePreviewUrl(
  value: string | null | undefined,
): string | null {
  const clean = value?.trim();
  if (!clean || /^asset:\/\//i.test(clean)) return null;
  return clean;
}

function referenceKindNoun(kind: VideoReferenceMediaIn["kind"]): string {
  if (kind === "image") return "图片";
  if (kind === "audio") return "音频";
  return "视频";
}

function referenceRefId(
  kind: VideoReferenceMediaIn["kind"],
  index: number,
): string {
  return `ref:${kind}:${index}`;
}

function referenceRefIndex(
  refId: string | null | undefined,
  kind: VideoReferenceMediaIn["kind"],
): number | null {
  const match = (refId ?? "").trim().toLowerCase().match(REFERENCE_REF_ID_RE);
  if (!match || match[1] !== kind) return null;
  const index = Number(match[2]);
  return Number.isInteger(index) && index > 0 ? index : null;
}

function referencePromptToken(
  item: Pick<VideoReferenceMediaIn, "kind" | "ref_id">,
  fallbackIndex = 1,
): string {
  const rawRefId = item.ref_id?.trim().toLowerCase() ?? "";
  const index = referenceRefIndex(rawRefId, item.kind);
  return `[${index ? rawRefId : referenceRefId(item.kind, fallbackIndex)}]`;
}

function referenceDisplayToken(
  item: Pick<VideoReferenceMediaIn, "kind" | "ref_id">,
  fallbackIndex = 1,
): string {
  const rawRefId = item.ref_id?.trim().toLowerCase() ?? "";
  const index = referenceRefIndex(rawRefId, item.kind) ?? fallbackIndex;
  return `@${referenceKindNoun(item.kind)}${index}`;
}

export function VideoParameterPanel({
  ...props
}: VideoParameterPanelProps) {
  return (
    <VideoParameterPanelView
      {...props}
      id="video-generation-settings"
    >
      视频生成参数
    </VideoParameterPanelView>
  );
}

function promptEnhancePreviewCandidateId(
  candidates: PromptEnhanceCandidate[],
  previewCandidateId: string,
  selectedId: string,
): string {
  if (candidates.some((candidate) => candidate.id === previewCandidateId)) {
    return previewCandidateId;
  }
  if (candidates.some((candidate) => candidate.id === selectedId)) {
    return selectedId;
  }
  return candidates[0]?.id ?? "";
}

function promptEnhanceChooserSubtitle({
  loading,
  candidateCount,
  autoApplied,
}: {
  loading: boolean;
  candidateCount: number;
  autoApplied: boolean;
}): string {
  if (candidateCount > 1) {
    return autoApplied
      ? `${candidateCount} 个候选，已应用推荐版`
      : `${candidateCount} 个候选，未自动替换`;
  }
  if (loading) return "按火山视频结构补动作、运镜和参考一致性";
  return autoApplied ? "已应用到描述" : "已保留原描述";
}

function promptEnhanceActionLabel(action: PromptEnhanceAction): string {
  if (action === "light_refine") return "轻度优化";
  if (action === "direct_pass") return "直接优化";
  if (action === "ask_first") return "需要补充";
  if (action === "keep_original") return "建议保留原稿";
  if (action === "optional_vc") return "可选改写";
  return "完整改写";
}

function PromptEnhanceLoadingState({ preview }: { preview: string }) {
  return <PromptEnhanceLoadingStateView preview={preview} />;
}

function PromptEnhanceCandidateCard({
  candidate,
  index,
  selected,
  previewing,
  onPreview,
}: {
  candidate: PromptEnhanceCandidate;
  index: number;
  selected: boolean;
  previewing: boolean;
  onPreview: () => void;
}) {
  return (
    <PromptEnhanceCandidateCardView
      candidate={candidate}
      index={index}
      selected={selected}
      previewing={previewing}
      actionLabel={promptEnhanceActionLabel(candidate.action)}
      onPreview={onPreview}
    />
  );
}

function PromptEnhanceCandidatePreview({
  candidate,
  selected,
  onApply,
  onCopy,
}: {
  candidate: PromptEnhanceCandidate;
  selected: boolean;
  onApply: () => void;
  onCopy: () => void;
}) {
  const applicable = canApplyPromptEnhanceCandidate(candidate);
  return (
    <PromptEnhanceCandidatePreviewView
      candidate={candidate}
      selected={selected}
      applicable={applicable}
      actionLabel={promptEnhanceActionLabel(candidate.action)}
      buttonText={promptEnhanceCandidateButtonText(candidate, selected)}
      footer={
        <>
        完整提示词 · {candidate.prompt.length.toLocaleString()} 字
        </>
      }
      onApply={onApply}
      onCopy={onCopy}
    />
  );
}

export function PromptEnhanceChooser({
  loading,
  preview,
  candidates,
  selectedId,
  onSelect,
  onDismiss,
  onReturnToEditor,
}: {
  loading: boolean;
  preview: string;
  candidates: PromptEnhanceCandidate[];
  selectedId: string;
  onSelect: (candidate: PromptEnhanceCandidate) => void;
  onDismiss: () => void;
  onReturnToEditor: () => void;
}) {
  const cleanPreview = cleanPromptEnhanceText(preview);
  const visibleCandidates = candidates;
  const firstCandidate = visibleCandidates[0];
  const [previewCandidateId, setPreviewCandidateId] = useState("");
  const effectivePreviewCandidateId = promptEnhancePreviewCandidateId(
    visibleCandidates,
    previewCandidateId,
    selectedId,
  );
  const previewCandidate =
    visibleCandidates.find(
      (candidate) => candidate.id === effectivePreviewCandidateId,
    ) ??
    firstCandidate ??
    null;
  const autoApplied =
    firstCandidate != null &&
    firstCandidate.id === selectedId &&
    shouldAutoApplyPromptEnhanceCandidate(firstCandidate);

  const copyCandidate = async (candidate: PromptEnhanceCandidate) => {
    try {
      await navigator.clipboard.writeText(candidate.prompt);
      toast.success("已复制提示词");
    } catch {
      toast.error("复制失败");
    }
  };

  return (
    <section className="overflow-hidden border-y border-[var(--border)] bg-transparent">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border-subtle)] px-3 py-2.5 sm:px-4">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]">
            {loading ? (
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Sparkles className="h-3.5 w-3.5" />
            )}
          </span>
          <span className="min-w-0">
            <span className="block type-body-sm font-semibold text-[var(--fg-0)]">
              {loading ? "提示词优化中" : "AI 优化结果"}
            </span>
            <span className="block truncate type-caption text-[var(--fg-2)]">
              {promptEnhanceChooserSubtitle({
                loading,
                candidateCount: visibleCandidates.length,
                autoApplied,
              })}
            </span>
          </span>
        </div>
        {!loading && (
          <div className="flex shrink-0 items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={onReturnToEditor}
              leftIcon={<PencilLine className="h-3.5 w-3.5" />}
            >
              回到编辑
            </Button>
            <IconButton
              variant="ghost"
              size="sm"
              onClick={onDismiss}
              aria-label="关闭优化结果"
              tooltip="关闭优化结果"
            >
              <X className="h-4 w-4" />
            </IconButton>
          </div>
        )}
      </header>

      {loading && <PromptEnhanceLoadingState preview={cleanPreview} />}

      {!loading && visibleCandidates.length > 0 && previewCandidate && (
        <div className="space-y-3 p-3 sm:p-4">
          <div className="flex items-center justify-between gap-3">
            <p className="type-caption font-medium text-[var(--fg-1)]">
              选择一个优化方向
            </p>
            <p className="type-caption text-[var(--fg-2)]">
              点击卡片切换完整预览
            </p>
          </div>
          <div className="flex gap-2 overflow-x-auto pb-1 lg:grid lg:grid-cols-3 lg:overflow-visible">
            {visibleCandidates.map((candidate, index) => (
              <PromptEnhanceCandidateCard
                key={candidate.id}
                candidate={candidate}
                index={index}
                selected={candidate.id === selectedId}
                previewing={candidate.id === previewCandidate.id}
                onPreview={() => setPreviewCandidateId(candidate.id)}
              />
            ))}
          </div>
          <PromptEnhanceCandidatePreview
            candidate={previewCandidate}
            selected={previewCandidate.id === selectedId}
            onApply={() => onSelect(previewCandidate)}
            onCopy={() => void copyCandidate(previewCandidate)}
          />
        </div>
      )}
    </section>
  );
}

export function ReferenceChip({
  item,
  active,
  onInsert,
  onPreview,
  onRemove,
}: {
  item: ReferenceDraft;
  active: boolean;
  onInsert: () => void;
  onPreview: () => void;
  onRemove: () => void;
}) {
  const displayToken = referenceDisplayToken(item);
  const anchorToken = referencePromptToken(item);
  return (
    <div
      className={cn(
        "relative flex h-24 w-[min(82vw,19rem)] max-w-[calc(100vw-3rem)] shrink-0 overflow-hidden rounded-[var(--radius-control)] border bg-[var(--bg-1)] type-caption text-[var(--fg-1)] transition-[background-color,border-color,box-shadow]",
        active
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
          : "border-[var(--border)]",
      )}
    >
      <button
        type="button"
        onClick={onPreview}
        title={`查看 ${displayToken} 预览`}
        aria-label={`查看 ${displayToken} 预览`}
        className="shrink-0 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/50"
      >
        <ReferenceThumbnail item={item} active={active} />
      </button>
      <button
        type="button"
        onClick={onInsert}
        title={
          active
            ? `已引用 ${displayToken}，提交时映射为 ${anchorToken}`
            : `插入 ${displayToken}`
        }
        className="flex min-w-0 flex-1 cursor-pointer flex-col justify-center gap-1 px-3 py-2.5 pr-9 text-left transition-colors hover:bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/50"
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 font-semibold text-[var(--fg-0)]">
            {displayToken}
          </span>
          <span className="min-w-0 truncate text-[var(--fg-2)]">
            {item.label}
          </span>
        </span>
        <span className="max-w-full truncate font-mono type-caption text-[var(--fg-2)]">
          {item.display}
        </span>
        <span className="type-caption text-[var(--fg-2)]">
          {active ? "已用于提示词" : "点击文字插入引用"}
        </span>
      </button>
      <button
        type="button"
        aria-label="移除参考素材"
        onClick={onRemove}
        className="absolute right-0 top-0 flex h-11 w-11 shrink-0 items-start justify-end rounded-bl-[var(--radius-control)] bg-[var(--bg-1)]/85 p-2 text-[var(--fg-2)] shadow-[var(--shadow-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]"
      >
        <XCircle className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function ReferenceThumbnail({
  item,
  active,
}: {
  item: ReferenceDraft;
  active: boolean;
}) {
  const previewUrl = cleanReferencePreviewUrl(item.previewUrl);
  const [failedPreviewUrl, setFailedPreviewUrl] = useState<string | null>(null);
  const failed = previewUrl != null && failedPreviewUrl === previewUrl;
  const showPreview = Boolean(previewUrl && !failed);
  return (
    <ReferenceThumbnailView
      item={item}
      active={active}
      className="relative flex h-24 w-32 shrink-0 overflow-hidden border-r border-[var(--border-subtle)] bg-[var(--bg-0)] text-[var(--fg-2)]"
      failed={failed}
      showPreview={showPreview}
      preview={
        <img
          src={previewUrl ?? ""}
          alt=""
          className="h-full w-full object-cover"
          loading="lazy"
          decoding="async"
          onError={() => setFailedPreviewUrl(previewUrl)}
        />
      }
    />
  );
}

function referenceMediaPreviewSources(item: ReferenceDraft): {
  mediaUrl: string | null;
  posterUrl: string | null;
} {
  const previewUrl = cleanReferencePreviewUrl(item.previewUrl);
  if (item.kind === "image") {
    return { mediaUrl: previewUrl, posterUrl: null };
  }

  const directMediaUrl = cleanReferencePreviewUrl(item.url);
  if (item.kind === "audio") {
    return {
      mediaUrl: directMediaUrl ?? previewUrl,
      posterUrl: null,
    };
  }

  const videoId = item.video_id?.trim();
  const mediaUrl = videoId
    ? videoBinaryUrl(videoId)
    : (directMediaUrl ?? previewUrl);
  return {
    mediaUrl,
    posterUrl:
      previewUrl && previewUrl !== mediaUrl ? previewUrl : null,
  };
}

function ReferenceMediaPreviewIcon({ item }: { item: ReferenceDraft }) {
  if (item.kind === "video") {
    return <VideoIcon className="h-8 w-8" aria-hidden="true" />;
  }
  if (item.kind === "audio") {
    return <AudioLines className="h-8 w-8" aria-hidden="true" />;
  }
  if (item.url) {
    return <Tags className="h-8 w-8" aria-hidden="true" />;
  }
  return <ImageIcon className="h-8 w-8" aria-hidden="true" />;
}

function ReferenceMediaPreviewContent({
  item,
  displayToken,
  mediaUrl,
  posterUrl,
  failed,
  onError,
}: {
  item: ReferenceDraft;
  displayToken: string;
  mediaUrl: string | null;
  posterUrl: string | null;
  failed: boolean;
  onError: () => void;
}) {
  const referenceNoun = referenceKindNoun(item.kind);

  if (!mediaUrl || failed) {
    return (
      <div
        role={failed ? "alert" : "status"}
        className="flex flex-col items-center justify-center gap-2 px-5 text-center text-[var(--fg-2)]"
      >
        <ReferenceMediaPreviewIcon item={item} />
        <p className="type-body-sm font-medium text-[var(--fg-1)]">
          {failed
            ? `${referenceNoun}预览加载失败`
            : `这个${referenceNoun}暂无可显示预览`}
        </p>
        <p className="max-w-md type-caption leading-5">
          {failed
            ? "确认素材仍可访问，或稍后重试。"
            : `官方${referenceNoun}素材可能只有素材 ID，暂时无法在这里直接预览。`}
        </p>
      </div>
    );
  }

  if (item.kind === "video") {
    return (
      <video
        src={mediaUrl}
        poster={posterUrl ?? undefined}
        controls
        playsInline
        preload="metadata"
        aria-label={`${displayToken} 视频预览`}
        className="h-full w-full object-contain"
        onError={onError}
      >
        当前浏览器不支持视频预览。
      </video>
    );
  }

  if (item.kind === "audio") {
    return (
      <div className="flex w-full max-w-2xl flex-col items-center gap-4 px-5 py-8">
        <AudioLines
          className="h-8 w-8 text-[var(--fg-2)]"
          aria-hidden="true"
        />
        <audio
          src={mediaUrl}
          controls
          preload="metadata"
          aria-label={`${displayToken} 音频预览`}
          className="w-full"
          onError={onError}
        >
          当前浏览器不支持音频预览。
        </audio>
      </div>
    );
  }

  return (
    <img
      src={mediaUrl}
      alt={`${displayToken} 图片预览`}
      className="h-full w-full object-contain"
      decoding="async"
      onError={onError}
    />
  );
}

export function ReferenceMediaPreviewDialog({
  item,
  onClose,
  onInsert,
}: {
  item: ReferenceDraft;
  onClose: () => void;
  onInsert: () => void;
}) {
  const dialogRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);
  const { mediaUrl, posterUrl } = referenceMediaPreviewSources(item);
  const [failedPreviewUrl, setFailedPreviewUrl] = useState<string | null>(null);
  const failed = mediaUrl != null && failedPreviewUrl === mediaUrl;
  const displayToken = referenceDisplayToken(item);
  const referenceNoun = referenceKindNoun(item.kind);

  useEffect(() => {
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const dialog = dialogRef.current;
    const focusFrame = window.requestAnimationFrame(() => {
      focusVideoWorkbenchElement(dialog, { preventScroll: true });
    });
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!isTopmostVideoDialog(dialog)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      trapVideoDialogFocus(event, dialog);
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("keydown", handleKeyDown);
      restoreVideoWorkbenchFocus(previousFocus, dialog);
    };
  }, []);

  return (
    <div
      className="mobile-dialog-shell mobile-perf-surface fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-[var(--surface-scrim)] backdrop-blur-md sm:items-center sm:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`reference-preview-${item._key}`}
        aria-describedby={`reference-preview-description-${item._key}`}
        tabIndex={-1}
        className="mobile-dialog-panel flex h-[var(--mobile-dialog-max-height)] w-full max-w-4xl flex-col overflow-hidden rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] sm:h-[min(760px,calc(100dvh-2.5rem))] sm:rounded-[var(--radius-panel)] sm:border-b landscape:max-sm:rounded-[var(--radius-panel)] landscape:max-sm:border-b"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-1)]/95 px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-2)]">
              {`参考${referenceNoun}`}
            </p>
            <h2
              id={`reference-preview-${item._key}`}
              className="mt-1 truncate type-body font-semibold text-[var(--fg-0)]"
            >
              {displayToken} · {item.label}
            </h2>
            <p
              id={`reference-preview-description-${item._key}`}
              className="mt-1 truncate font-mono type-caption text-[var(--fg-2)]"
            >
              {item.display}
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-11 w-11 shrink-0 px-0"
            onClick={onClose}
            aria-label="关闭参考素材预览"
          >
            <XCircle className="h-4 w-4" />
          </Button>
        </header>
        <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto bg-[var(--bg-0)] p-3 sm:p-5">
          <div className="flex h-full min-h-0 items-center justify-center overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] sm:min-h-[18rem]">
            <ReferenceMediaPreviewContent
              item={item}
              displayToken={displayToken}
              mediaUrl={mediaUrl}
              posterUrl={posterUrl}
              failed={failed}
              onError={() => setFailedPreviewUrl(mediaUrl)}
            />
          </div>
        </div>
        <footer className="mobile-dialog-footer flex shrink-0 flex-col items-stretch gap-2 border-t border-[var(--border)] bg-[var(--bg-1)]/88 px-4 py-3 min-[390px]:flex-row min-[390px]:items-center min-[390px]:justify-between sm:px-5">
          <span className="truncate type-caption text-[var(--fg-2)]">
            提交时映射为 {referencePromptToken(item)}
          </span>
          <div className="grid shrink-0 grid-cols-2 gap-2">
            <Button variant="outline" size="sm" onClick={onClose}>
              关闭
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={onInsert}
              leftIcon={<Tags className="h-3.5 w-3.5" />}
            >
              插入引用
            </Button>
          </div>
        </footer>
      </section>
    </div>
  );
}
