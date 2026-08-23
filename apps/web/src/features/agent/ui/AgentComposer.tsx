"use client";

import { Images, Paperclip, Send, Settings2, Square } from "lucide-react";
import Link from "next/link";
import {
  type ClipboardEvent,
  type KeyboardEvent,
  useEffect,
  useRef,
  useState,
} from "react";
import type { GenerationSummary } from "@/features/assets";
import { DesktopPopover } from "@/components/ui/composer/desktop/DesktopPopover";
import { useComposerAttachmentDnd } from "@/components/ui/composer/shared/useComposerAttachmentDnd";
import { Button, IconButton } from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile";
import type { AttachmentRole } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  AGENT_MAX_REFERENCES,
  type AgentDraft,
  type AgentDraftAttachment,
  type AgentImageDefaults,
} from "../model/contracts";
import { AgentAttachmentTray } from "./AgentAttachmentTray";
import { AgentComposerSettings } from "./AgentComposerSettings";
import { AgentReferencePicker } from "./AgentReferencePicker";

export function AgentComposer({
  platform,
  draft,
  submitting,
  runActive,
  stopping,
  error,
  errorAction,
  assetItems,
  assetsLoading,
  assetsHaveMore,
  onLoadMoreAssets,
  onTextChange,
  onDraftChange,
  onDefaultsChange,
  onUpload,
  onAddAttachment,
  onRemoveAttachment,
  onMoveAttachment,
  onRoleChange,
  onPreviewAttachment,
  onPickAsset,
  onSubmit,
  onStop,
  onError,
  onMetricsChange,
}: {
  platform: "desktop" | "mobile";
  draft: AgentDraft;
  submitting: boolean;
  runActive: boolean;
  stopping: boolean;
  error: string | null;
  errorAction: { href: string; label: string } | null;
  assetItems: GenerationSummary[];
  assetsLoading: boolean;
  assetsHaveMore: boolean;
  onLoadMoreAssets: () => void;
  onTextChange: (text: string) => void;
  onDraftChange: (patch: Partial<AgentDraft>) => void;
  onDefaultsChange: (patch: Partial<AgentImageDefaults>) => void;
  onUpload: (file: File, signal: AbortSignal) => Promise<AgentDraftAttachment>;
  onAddAttachment: (attachment: AgentDraftAttachment) => boolean;
  onRemoveAttachment: (imageId: string) => void;
  onMoveAttachment: (imageId: string, direction: -1 | 1) => void;
  onRoleChange: (imageId: string, role: AttachmentRole) => void;
  onPreviewAttachment: (attachment: AgentDraftAttachment) => void;
  onPickAsset: (item: GenerationSummary) => void;
  onSubmit: () => void;
  onStop: () => void;
  onError: (message: string | null) => void;
  onMetricsChange?: (height: number) => void;
}) {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [isDragActive, setIsDragActive] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const dragDepthRef = useRef(0);
  const settingsAnchorRef = useRef<HTMLDivElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const disabled = submitting || runActive || stopping;

  const dnd = useComposerAttachmentDnd({
    fileInputRef,
    dragDepthRef,
    setIsUploading,
    setIsDragActive,
    setExpanded: () => {},
    uploadAttachment: (file, options) => onUpload(file, options.signal),
    addAttachment: onAddAttachment,
    getAttachmentCount: () => draft.attachments.length,
    setError: onError,
    limit: AGENT_MAX_REFERENCES,
    attachmentNoun: "参考图",
  });

  useEffect(() => {
    const root = rootRef.current;
    if (!root || !onMetricsChange) return;
    const update = () => onMetricsChange(root.getBoundingClientRect().height);
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(root);
    return () => observer.disconnect();
  }, [onMetricsChange]);

  const canSubmit =
    !disabled &&
    !isUploading &&
    (draft.text.trim().length > 0 || draft.attachments.length > 0);
  const summary = draft.allowImage
    ? `${draft.imageDefaults.count} 张 · ${draft.imageDefaults.aspect_ratio} · ${draft.imageDefaults.quality.toUpperCase()}`
    : "仅文本";

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      (event.metaKey || event.ctrlKey) &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      if (canSubmit) onSubmit();
    }
  };

  const selectedIds = new Set(
    draft.attachments.map((attachment) => attachment.imageId),
  );
  const settings = (
    <AgentComposerSettings
      draft={draft}
      disabled={disabled}
      onAllowImageChange={(allowImage) => onDraftChange({ allowImage })}
      onReasoningEffortChange={(reasoningEffort) =>
        onDraftChange({ reasoningEffort })
      }
      onDefaultsChange={onDefaultsChange}
    />
  );

  return (
    <>
      <div
        ref={rootRef}
        className={cn(
          "z-[var(--z-composer)] pointer-events-none",
          platform === "desktop"
            ? "absolute inset-x-0 bottom-0 px-3 pb-[max(var(--space-4),env(safe-area-inset-bottom,0px))]"
            : "safe-x-page fixed inset-x-0 bottom-[var(--agent-mobile-nav-offset,var(--mobile-tabbar-height))] pb-[max(var(--space-1),env(safe-area-inset-bottom,0px))]",
        )}
      >
        <div
          data-testid="agent-composer"
          onDragEnter={dnd.handleDragEnter}
          onDragOver={dnd.handleDragOver}
          onDragLeave={dnd.handleDragLeave}
          onDrop={dnd.handleDrop}
          className={cn(
            "surface-panel pointer-events-auto mx-auto w-full max-w-[var(--content-composer)] overflow-hidden bg-[var(--surface-glass)]",
            isDragActive && "border-accent-border shadow-[var(--shadow-amber)]",
          )}
        >
          <AgentAttachmentTray
            attachments={draft.attachments}
            disabled={disabled}
            onPreview={onPreviewAttachment}
            onRemove={onRemoveAttachment}
            onMove={onMoveAttachment}
            onRoleChange={onRoleChange}
          />

          <div className="flex min-h-14 items-end gap-1.5 p-2">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              multiple
              className="sr-only"
              onChange={dnd.handleFileInput}
            />
            <IconButton
              size="md"
              variant="ghost"
              onClick={dnd.openFilePicker}
              disabled={disabled || isUploading}
              loading={isUploading}
              aria-label="上传参考图"
              tooltip="上传参考图"
            >
              <Paperclip className="h-4 w-4" aria-hidden />
            </IconButton>
            <IconButton
              size="md"
              variant="ghost"
              onClick={() => setPickerOpen(true)}
              disabled={
                disabled || draft.attachments.length >= AGENT_MAX_REFERENCES
              }
              aria-label="从素材选择参考图"
              tooltip="选择参考图"
            >
              <Images className="h-4 w-4" aria-hidden />
            </IconButton>

            <textarea
              value={draft.text}
              onChange={(event) => onTextChange(event.target.value)}
              onPaste={(event: ClipboardEvent<HTMLTextAreaElement>) => {
                void dnd.handlePaste(event);
              }}
              onKeyDown={handleKeyDown}
              disabled={disabled}
              rows={1}
              maxLength={10_000}
              aria-label="发送给 Agent"
              placeholder="描述目标"
              className="max-h-36 min-h-11 min-w-0 flex-1 resize-none rounded-[var(--radius-control)] bg-transparent px-2 py-2.5 type-body text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] disabled:opacity-60"
            />

            <div ref={settingsAnchorRef}>
              <IconButton
                size="md"
                variant={settingsOpen ? "secondary" : "ghost"}
                onClick={() => setSettingsOpen((open) => !open)}
                aria-label="Agent 设置"
                aria-expanded={settingsOpen}
                tooltip="Agent 设置"
              >
                <Settings2 className="h-4 w-4" aria-hidden />
              </IconButton>
            </div>

            {runActive ? (
              <Button
                variant="danger"
                size="md"
                onClick={onStop}
                loading={stopping}
                aria-label="停止 Agent 运行"
                className="h-11 w-11 px-0"
              >
                <Square className="h-4 w-4" fill="currentColor" aria-hidden />
              </Button>
            ) : (
              <Button
                variant="primary"
                size="md"
                onClick={onSubmit}
                disabled={!canSubmit}
                loading={submitting}
                aria-label="发送"
                className="h-11 w-11 px-0"
              >
                <Send className="h-4 w-4" aria-hidden />
              </Button>
            )}
          </div>

          <div className="flex min-h-8 items-center justify-between gap-2 border-t border-[var(--border-subtle)] px-3 py-1.5">
            <span className="type-caption text-[var(--fg-2)]">{summary}</span>
            <span className="type-caption tabular-nums text-[var(--fg-3)]">
              {draft.text.length} / 10000
            </span>
          </div>
          <AgentComposerError error={error} action={errorAction} />
        </div>
      </div>

      {platform === "desktop" ? (
        <DesktopPopover
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
          anchorRef={settingsAnchorRef}
          ariaLabel="Agent 设置"
          align="right"
          maxHeight="min(680px, calc(100dvh - 32px))"
          className="w-[420px] p-0"
        >
          {settings}
        </DesktopPopover>
      ) : (
        <BottomSheet
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
          ariaLabel="Agent 设置"
          snapPoints={["82%"]}
        >
          {settings}
        </BottomSheet>
      )}

      <AgentReferencePicker
        open={pickerOpen}
        items={assetItems}
        selectedIds={selectedIds}
        loading={assetsLoading}
        hasMore={assetsHaveMore}
        onLoadMore={onLoadMoreAssets}
        onSelect={onPickAsset}
        onClose={() => setPickerOpen(false)}
      />
    </>
  );
}

function AgentComposerError({
  error,
  action,
}: {
  error: string | null;
  action: { href: string; label: string } | null;
}) {
  if (!error) return null;
  return (
    <div
      role="alert"
      className="flex min-h-10 items-center gap-2 border-t border-danger-border bg-danger-soft px-3 py-2 type-caption text-[var(--danger-fg)]"
    >
      <span className="min-w-0 flex-1">{error}</span>
      {action ? (
        <Link
          href={action.href}
          className="shrink-0 rounded-[var(--radius-control)] px-2 py-1 font-medium text-[var(--fg-0)] hover:bg-[var(--bg-2)]"
        >
          {action.label}
        </Link>
      ) : null}
    </div>
  );
}
