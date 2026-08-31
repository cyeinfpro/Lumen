"use client";

import {
  FilePlus2,
  Globe2,
  Images,
  Paperclip,
  Send,
  Settings2,
  Square,
} from "lucide-react";
import Link from "next/link";
import {
  type ClipboardEvent,
  type KeyboardEvent,
  useEffect,
  useLayoutEffect,
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
  AGENT_MAX_FILES,
  AGENT_MAX_REFERENCES,
  type AgentDraft,
  type AgentDraftAttachment,
  type AgentDraftFile,
  type AgentImageDefaults,
} from "../model/contracts";
import { AgentAttachmentTray } from "./AgentAttachmentTray";
import { AgentComposerSettings } from "./AgentComposerSettings";
import { AgentFileTray } from "./AgentFileTray";
import { AgentReferencePicker } from "./AgentReferencePicker";
import { useAgentTextFiles } from "./useAgentTextFiles";

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
  onAddFile,
  onRemoveFile,
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
  onAddFile: (file: AgentDraftFile) => boolean;
  onRemoveFile: (name: string) => void;
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
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const dragDepthRef = useRef(0);
  const settingsAnchorRef = useRef<HTMLDivElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const disabled = submitting || stopping;

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
  const {
    handleDrop: handleComposerDrop,
    handleFileInput: handleTextFileInput,
    isReadingFiles,
    textFileInputRef,
  } = useAgentTextFiles({
    currentCount: draft.files.length,
    onAddFile,
    onError,
    dragDepthRef,
    setDragActive: setIsDragActive,
    onFallbackDrop: dnd.handleDrop,
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

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(144, Math.max(44, textarea.scrollHeight))}px`;
  }, [draft.text, platform]);

  const canSubmit =
    !disabled &&
    !runActive &&
    !isUploading &&
    !isReadingFiles &&
    (draft.text.trim().length > 0 ||
      draft.attachments.length > 0 ||
      draft.files.length > 0);
  const summary = agentDraftSummary(draft);

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
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
      onAllowWebSearchChange={(allowWebSearch) =>
        onDraftChange({ allowWebSearch })
      }
      onAllowFileToolsChange={(allowFileTools) =>
        onDraftChange({ allowFileTools })
      }
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
          agentComposerPosition(platform),
        )}
      >
        <div
          data-testid="agent-composer"
          onDragEnter={dnd.handleDragEnter}
          onDragOver={dnd.handleDragOver}
          onDragLeave={dnd.handleDragLeave}
          onDrop={handleComposerDrop}
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
          <AgentFileTray
            files={draft.files}
            disabled={disabled}
            onRemove={onRemoveFile}
          />

          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            multiple
            className="sr-only"
            aria-label="选择要上传的参考图文件"
            onChange={dnd.handleFileInput}
          />
          <input
            ref={textFileInputRef}
            type="file"
            accept=".txt,.md,.csv,.json,.xml,.yaml,.yml,.js,.jsx,.ts,.tsx,.css,.html,.py,.sql,.sh,.toml,.ini,.log,text/*,application/json,application/xml,application/yaml,application/x-yaml,application/javascript,application/typescript,application/sql"
            multiple
            className="sr-only"
            aria-label="选择要添加的文本文件"
            onChange={handleTextFileInput}
          />
          <AgentComposerInputRow
            text={draft.text}
            runActive={runActive}
            disabled={disabled}
            settingsOpen={settingsOpen}
            submitting={submitting}
            stopping={stopping}
            canSubmit={canSubmit}
            textareaRef={textareaRef}
            settingsAnchorRef={settingsAnchorRef}
            onTextChange={onTextChange}
            onPaste={(event) => void dnd.handlePaste(event)}
            onKeyDown={handleKeyDown}
            onToggleSettings={() => setSettingsOpen((open) => !open)}
            onSubmit={onSubmit}
            onStop={onStop}
          />
          <AgentComposerToolbar
            draft={draft}
            runActive={runActive}
            disabled={disabled}
            isUploading={isUploading}
            isReadingFiles={isReadingFiles}
            summary={summary}
            onOpenImagePicker={dnd.openFilePicker}
            onOpenAssetPicker={() => setPickerOpen(true)}
            onOpenTextFilePicker={() => {
              if (!draft.allowFileTools) onDraftChange({ allowFileTools: true });
              textFileInputRef.current?.click();
            }}
            onToggleWebSearch={() =>
              onDraftChange({ allowWebSearch: !draft.allowWebSearch })
            }
          />
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

function agentComposerPosition(platform: "desktop" | "mobile"): string {
  return platform === "desktop"
    ? "absolute inset-x-0 bottom-0 px-3 pb-[max(var(--space-4),env(safe-area-inset-bottom,0px))]"
    : "safe-x-page fixed inset-x-0 bottom-[var(--agent-mobile-nav-offset,var(--mobile-tabbar-height))] pb-[max(var(--space-1),env(safe-area-inset-bottom,0px))]";
}

function agentDraftSummary(draft: AgentDraft): string {
  const tools: string[] = [];
  if (draft.attachments.length > 0) {
    tools.push(`本轮输入 ${draft.attachments.length} 张`);
  }
  if (draft.allowWebSearch) tools.push("联网");
  if (draft.files.length > 0) tools.push(`文件 ${draft.files.length}`);
  tools.push(
    draft.allowImage
      ? `${draft.imageDefaults.count} 张 · ${draft.imageDefaults.aspect_ratio} · ${draft.imageDefaults.quality.toUpperCase()}`
      : "仅文本",
  );
  return tools.join(" · ");
}

function AgentComposerInputRow({
  text,
  runActive,
  disabled,
  settingsOpen,
  submitting,
  stopping,
  canSubmit,
  textareaRef,
  settingsAnchorRef,
  onTextChange,
  onPaste,
  onKeyDown,
  onToggleSettings,
  onSubmit,
  onStop,
}: {
  text: string;
  runActive: boolean;
  disabled: boolean;
  settingsOpen: boolean;
  submitting: boolean;
  stopping: boolean;
  canSubmit: boolean;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  settingsAnchorRef: React.RefObject<HTMLDivElement | null>;
  onTextChange: (text: string) => void;
  onPaste: (event: ClipboardEvent<HTMLTextAreaElement>) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onToggleSettings: () => void;
  onSubmit: () => void;
  onStop: () => void;
}) {
  return (
    <div className="flex min-h-14 items-end gap-1.5 p-2">
      <textarea
        ref={textareaRef}
        value={text}
        onChange={(event) => onTextChange(event.target.value)}
        onPaste={onPaste}
        onKeyDown={onKeyDown}
        disabled={disabled}
        rows={1}
        maxLength={10_000}
        aria-label="发送给 Agent"
        placeholder={runActive ? "准备下一轮消息" : "描述目标"}
        className="max-h-36 min-h-11 min-w-0 flex-1 resize-none overflow-y-auto rounded-[var(--radius-control)] bg-transparent px-2 py-2.5 type-body text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] disabled:opacity-60"
      />
      <div ref={settingsAnchorRef}>
        <IconButton
          size="md"
          variant={settingsOpen ? "secondary" : "ghost"}
          onClick={onToggleSettings}
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
  );
}

function AgentComposerToolbar({
  draft,
  runActive,
  disabled,
  isUploading,
  isReadingFiles,
  summary,
  onOpenImagePicker,
  onOpenAssetPicker,
  onOpenTextFilePicker,
  onToggleWebSearch,
}: {
  draft: AgentDraft;
  runActive: boolean;
  disabled: boolean;
  isUploading: boolean;
  isReadingFiles: boolean;
  summary: string;
  onOpenImagePicker: () => void;
  onOpenAssetPicker: () => void;
  onOpenTextFilePicker: () => void;
  onToggleWebSearch: () => void;
}) {
  const webSearchLabel = draft.allowWebSearch ? "关闭联网搜索" : "开启联网搜索";
  return (
    <div className="flex min-h-10 items-center gap-1 border-t border-[var(--border-subtle)] px-2 py-1">
      <IconButton
        size="sm"
        variant="ghost"
        onClick={onOpenImagePicker}
        disabled={disabled || isUploading}
        loading={isUploading}
        aria-label="上传参考图"
        tooltip="上传参考图"
      >
        <Paperclip className="h-4 w-4" aria-hidden />
      </IconButton>
      <IconButton
        size="sm"
        variant="ghost"
        onClick={onOpenAssetPicker}
        disabled={disabled || draft.attachments.length >= AGENT_MAX_REFERENCES}
        aria-label="从素材选择参考图"
        tooltip="选择参考图"
      >
        <Images className="h-4 w-4" aria-hidden />
      </IconButton>
      <IconButton
        size="sm"
        variant={draft.files.length > 0 ? "secondary" : "ghost"}
        onClick={onOpenTextFilePicker}
        disabled={disabled || draft.files.length >= AGENT_MAX_FILES}
        loading={isReadingFiles}
        aria-label="添加文本文件"
        tooltip="添加文本文件"
      >
        <FilePlus2 className="h-4 w-4" aria-hidden />
      </IconButton>
      <IconButton
        size="sm"
        variant={draft.allowWebSearch ? "secondary" : "ghost"}
        onClick={onToggleWebSearch}
        disabled={disabled}
        aria-label={webSearchLabel}
        aria-pressed={draft.allowWebSearch}
        tooltip={draft.allowWebSearch ? "联网搜索已开启" : "联网搜索"}
      >
        <Globe2 className="h-4 w-4" aria-hidden />
      </IconButton>
      <span className="min-w-0 flex-1 truncate px-1 type-caption text-[var(--fg-2)]">
        {runActive ? "下一轮 · " : ""}{summary}
      </span>
      <span className="hidden shrink-0 type-caption tabular-nums text-[var(--fg-3)] sm:inline">
        {draft.text.length} / 10000
      </span>
    </div>
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
