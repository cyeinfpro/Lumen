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
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type { GenerationSummary } from "@/features/assets";
import { DesktopPopover } from "@/components/ui/composer/desktop/DesktopPopover";
import { useComposerAttachmentDnd } from "@/components/ui/composer/shared/useComposerAttachmentDnd";
import { useComposerCostEstimate } from "@/components/ui/composer/shared/useComposerCostEstimate";
import { Button, ConfirmDialog, IconButton, Select } from "@/components/ui/primitives";
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
  type AgentModelOption,
} from "../model/contracts";
import { AgentComposerSettings } from "./AgentComposerSettings";
import { AgentMediaDrawer } from "./AgentMediaDrawer";
import { AgentReferencePicker } from "./AgentReferencePicker";
import { useAgentTextFiles } from "./useAgentTextFiles";

export type AgentCapabilityKind = "visual" | "web" | "file";

export interface AgentCapabilityAction {
  kind: AgentCapabilityKind;
  prompt: string;
}

export interface AgentComposerHandle {
  startCapability: (action: AgentCapabilityAction) => void;
}

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
  imageGenerationAvailable,
  defaultModel,
  modelOptions,
  ref,
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
  imageGenerationAvailable: boolean;
  defaultModel: string | null;
  modelOptions: AgentModelOption[];
  ref?: React.Ref<AgentComposerHandle>;
}) {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [isDragActive, setIsDragActive] = useState(false);
  const [confirmDisableFilesOpen, setConfirmDisableFilesOpen] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const dragDepthRef = useRef(0);
  const settingsAnchorRef = useRef<HTMLDivElement | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const pendingCapabilityRef = useRef<{
    kind: "visual" | "file";
    prompt: string;
    draftText: string;
  } | null>(null);
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

  const startCapability = useCallback(
    (action: AgentCapabilityAction) => {
      if (action.kind === "web") {
        onDraftChange({ allowWebSearch: true });
        onTextChange(action.prompt);
        textareaRef.current?.focus();
        return;
      }
      if (action.kind === "file") {
        if (draft.files.length > 0) {
          onDraftChange({ allowFileTools: true });
          onTextChange(action.prompt);
          textareaRef.current?.focus();
          return;
        }
        pendingCapabilityRef.current = {
          kind: "file",
          prompt: action.prompt,
          draftText: draft.text,
        };
        onDraftChange({ allowFileTools: true });
        textFileInputRef.current?.click();
        return;
      }
      if (!imageGenerationAvailable) {
        onError("生图工具未就绪，可先上传图片进行视觉分析。");
      }
      if (draft.attachments.length > 0) {
        onDraftChange({ allowImage: imageGenerationAvailable });
        onTextChange(action.prompt);
        textareaRef.current?.focus();
        return;
      }
      pendingCapabilityRef.current = {
        kind: "visual",
        prompt: action.prompt,
        draftText: draft.text,
      };
      if (imageGenerationAvailable) onDraftChange({ allowImage: true });
      dnd.openFilePicker();
    },
    [
      dnd,
      draft.attachments.length,
      draft.files.length,
      draft.text,
      imageGenerationAvailable,
      onDraftChange,
      onError,
      onTextChange,
      textFileInputRef,
    ],
  );

  useImperativeHandle(ref, () => ({ startCapability }), [startCapability]);

  useEffect(() => {
    const pending = pendingCapabilityRef.current;
    if (!pending || draft.text !== pending.draftText) return;
    const ready =
      (pending.kind === "visual" && draft.attachments.length > 0) ||
      (pending.kind === "file" && draft.files.length > 0);
    if (!ready) return;
    pendingCapabilityRef.current = null;
    onTextChange(pending.prompt);
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  }, [draft.attachments.length, draft.files.length, draft.text, onTextChange]);

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
  const summary = agentDraftSummary(draft, imageGenerationAvailable);
  const imageExecutionEnabled = draft.allowImage && imageGenerationAvailable;
  const costEstimate = useComposerCostEstimate({
    mode: imageExecutionEnabled ? "image" : "chat",
    quality: draft.imageDefaults.quality,
    aspect: draft.imageDefaults.aspect_ratio,
    count: draft.imageDefaults.count,
  });

  const requestFileToolsChange = useCallback(
    (allowFileTools: boolean) => {
      if (allowFileTools || draft.files.length === 0) {
        onDraftChange({ allowFileTools });
        return;
      }
      setConfirmDisableFilesOpen(true);
    },
    [draft.files.length, onDraftChange],
  );
  const confirmDisableFileTools = useCallback(() => {
    for (const file of draft.files) onRemoveFile(file.name);
    onDraftChange({ allowFileTools: false });
    setConfirmDisableFilesOpen(false);
  }, [draft.files, onDraftChange, onRemoveFile]);

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
      imageGenerationAvailable={imageGenerationAvailable}
      onAllowImageChange={(allowImage) => onDraftChange({ allowImage })}
      onAllowWebSearchChange={(allowWebSearch) =>
        onDraftChange({ allowWebSearch })
      }
      defaultModel={defaultModel}
      modelOptions={modelOptions}
      onModelChange={(model) => onDraftChange({ model })}
      onAllowFileToolsChange={requestFileToolsChange}
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
          <AgentMediaDrawer
            attachments={draft.attachments}
            files={draft.files}
            disabled={disabled}
            onPreview={onPreviewAttachment}
            onRemoveAttachment={onRemoveAttachment}
            onMoveAttachment={onMoveAttachment}
            onRoleChange={onRoleChange}
            onRemoveFile={onRemoveFile}
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
          <AgentExecutionSummary
            draft={draft}
            disabled={disabled}
            imageExecutionEnabled={imageExecutionEnabled}
            runActive={runActive}
            summary={summary}
            costLabel={costEstimate.label}
            costWarning={costEstimate.warning}
            costLoading={costEstimate.loading}
            onDefaultsChange={onDefaultsChange}
          />
          <AgentComposerToolbar
            draft={draft}
            disabled={disabled}
            isUploading={isUploading}
            isReadingFiles={isReadingFiles}
            imageGenerationAvailable={imageGenerationAvailable}
            onOpenImagePicker={dnd.openFilePicker}
            onOpenAssetPicker={() => setPickerOpen(true)}
            onOpenTextFilePicker={() => {
              if (!draft.allowFileTools) onDraftChange({ allowFileTools: true });
              textFileInputRef.current?.click();
            }}
            onToggleWebSearch={() =>
              onDraftChange({ allowWebSearch: !draft.allowWebSearch })
            }
            onToggleImage={() =>
              onDraftChange({ allowImage: !draft.allowImage })
            }
            onToggleFileTools={() =>
              requestFileToolsChange(!draft.allowFileTools)
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
      <ConfirmDialog
        open={confirmDisableFilesOpen}
        onOpenChange={setConfirmDisableFilesOpen}
        title="关闭文件工具？"
        description={`关闭后将移除本轮已添加的 ${draft.files.length} 个文件。`}
        confirmText="关闭并移除"
        tone="danger"
        onConfirm={confirmDisableFileTools}
      />
    </>
  );
}

function agentComposerPosition(platform: "desktop" | "mobile"): string {
  return platform === "desktop"
    ? "absolute inset-x-0 bottom-0 px-3 pb-[max(var(--space-4),env(safe-area-inset-bottom,0px))]"
    : "safe-x-page fixed inset-x-0 bottom-[var(--agent-mobile-nav-offset,var(--mobile-tabbar-height))] pb-[max(var(--space-1),env(safe-area-inset-bottom,0px))]";
}

function agentDraftSummary(
  draft: AgentDraft,
  imageGenerationAvailable: boolean,
): string {
  const tools: string[] = [];
  if (draft.attachments.length > 0) {
    tools.push(`本轮输入 ${draft.attachments.length} 张`);
  }
  if (draft.allowWebSearch) tools.push("联网");
  if (draft.files.length > 0) tools.push(`文件 ${draft.files.length}`);
  tools.push(
    draft.allowImage && imageGenerationAvailable
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

const AGENT_SUMMARY_ASPECT_RATIOS: AgentImageDefaults["aspect_ratio"][] = [
  "1:1",
  "16:9",
  "9:16",
  "4:5",
  "3:4",
  "4:3",
  "3:2",
  "2:3",
  "21:9",
  "9:21",
  "10:7",
  "7:10",
];

function AgentExecutionSummary({
  draft,
  disabled,
  imageExecutionEnabled,
  runActive,
  summary,
  costLabel,
  costWarning,
  costLoading,
  onDefaultsChange,
}: {
  draft: AgentDraft;
  disabled: boolean;
  imageExecutionEnabled: boolean;
  runActive: boolean;
  summary: string;
  costLabel: string | null;
  costWarning: boolean;
  costLoading: boolean;
  onDefaultsChange: (patch: Partial<AgentImageDefaults>) => void;
}) {
  const defaults = draft.imageDefaults;
  return (
    <div
      data-testid="agent-execution-summary"
      className="flex min-h-11 min-w-0 items-center gap-2 border-t border-[var(--border-subtle)] px-2 py-1"
    >
      <div className="scrollbar-thin flex min-w-0 flex-1 items-center gap-1.5 overflow-x-auto">
        <span className="min-w-24 flex-1 truncate px-1 type-caption text-[var(--fg-2)]">
          {runActive ? "下一轮 · " : ""}
          {summary}
        </span>
        {imageExecutionEnabled ? (
          <>
            <SummarySelect
              label="执行图片数量"
              value={String(defaults.count)}
              disabled={disabled}
              width="w-[5.25rem]"
              onChange={(value) => onDefaultsChange({ count: Number(value) })}
              options={[1, 2, 3, 4].map((count) => ({
                value: String(count),
                label: `${count} 张`,
              }))}
            />
            <SummarySelect
              label="执行图片比例"
              value={defaults.aspect_ratio}
              disabled={disabled}
              width="w-[5.5rem]"
              onChange={(value) =>
                onDefaultsChange({
                  aspect_ratio: value as AgentImageDefaults["aspect_ratio"],
                })
              }
              options={AGENT_SUMMARY_ASPECT_RATIOS.map((aspect) => ({
                value: aspect,
                label: aspect,
              }))}
            />
            <SummarySelect
              label="执行图片分辨率"
              value={defaults.quality}
              disabled={disabled}
              width="w-[5rem]"
              onChange={(value) =>
                onDefaultsChange({
                  quality: value as AgentImageDefaults["quality"],
                })
              }
              options={["1k", "2k", "4k"].map((quality) => ({
                value: quality,
                label: quality.toUpperCase(),
              }))}
            />
            <SummarySelect
              label="执行渲染质量"
              value={defaults.render_quality}
              disabled={disabled}
              width="w-[5.5rem]"
              onChange={(value) =>
                onDefaultsChange({
                  render_quality: value as AgentImageDefaults["render_quality"],
                })
              }
              options={[
                { value: "auto", label: "自动" },
                { value: "low", label: "草稿" },
                { value: "medium", label: "标准" },
                { value: "high", label: "精细" },
              ]}
            />
            <SummarySelect
              label="执行图片背景"
              value={defaults.background}
              disabled={disabled}
              width="w-[5.75rem]"
              onChange={(value) =>
                onDefaultsChange({
                  background: value as AgentImageDefaults["background"],
                })
              }
              options={[
                { value: "auto", label: "自动背景" },
                { value: "opaque", label: "不透明" },
                { value: "transparent", label: "透明底" },
              ]}
            />
          </>
        ) : null}
      </div>
      <span
        aria-live="polite"
        data-agent-cost-estimate
        className={cn(
          "min-w-28 shrink-0 text-right type-caption tabular-nums",
          costWarning
            ? "text-[var(--warning-fg)]"
            : "text-[var(--fg-2)]",
          costLoading && "opacity-70",
        )}
      >
        {costLabel ?? ""}
      </span>
    </div>
  );
}

function SummarySelect({
  label,
  value,
  disabled,
  width,
  options,
  onChange,
}: {
  label: string;
  value: string;
  disabled: boolean;
  width: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <Select
      aria-label={label}
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      wrapperClassName={cn("shrink-0", width)}
      className="h-8 min-h-8 py-0 pl-2 pr-7 type-caption max-sm:min-h-11"
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </Select>
  );
}

function AgentComposerToolbar({
  draft,
  disabled,
  isUploading,
  isReadingFiles,
  imageGenerationAvailable,
  onOpenImagePicker,
  onOpenAssetPicker,
  onOpenTextFilePicker,
  onToggleWebSearch,
  onToggleImage,
  onToggleFileTools,
}: {
  draft: AgentDraft;
  disabled: boolean;
  isUploading: boolean;
  isReadingFiles: boolean;
  imageGenerationAvailable: boolean;
  onOpenImagePicker: () => void;
  onOpenAssetPicker: () => void;
  onOpenTextFilePicker: () => void;
  onToggleWebSearch: () => void;
  onToggleImage: () => void;
  onToggleFileTools: () => void;
}) {
  return (
    <div
      data-testid="agent-composer-toolbar"
      className="border-t border-[var(--border-subtle)] px-2 py-1.5"
    >
      <div className="scrollbar-thin flex min-w-0 items-center gap-1 overflow-x-auto pb-0.5">
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
          variant="ghost"
          onClick={onOpenTextFilePicker}
          disabled={disabled || draft.files.length >= AGENT_MAX_FILES}
          loading={isReadingFiles}
          aria-label="添加文本文件"
          tooltip="添加文本文件"
        >
          <FilePlus2 className="h-4 w-4" aria-hidden />
        </IconButton>
        <span className="mx-0.5 h-5 w-px shrink-0 bg-[var(--border-subtle)]" aria-hidden />
        <AgentToolToggle
          icon={<Globe2 className="h-3.5 w-3.5" aria-hidden />}
          label="联网"
          ariaLabel={draft.allowWebSearch ? "关闭联网搜索" : "开启联网搜索"}
          checked={draft.allowWebSearch}
          disabled={disabled}
          onClick={onToggleWebSearch}
        />
        <AgentToolToggle
          icon={<Images className="h-3.5 w-3.5" aria-hidden />}
          label="生图"
          checked={draft.allowImage && imageGenerationAvailable}
          disabled={disabled || !imageGenerationAvailable}
          onClick={onToggleImage}
          unavailable={!imageGenerationAvailable}
        />
        <AgentToolToggle
          icon={<FilePlus2 className="h-3.5 w-3.5" aria-hidden />}
          label="文件"
          checked={draft.allowFileTools && draft.files.length > 0}
          disabled={disabled}
          unavailable={draft.files.length === 0}
          unavailableLabel="待文件"
          onClick={draft.files.length > 0 ? onToggleFileTools : onOpenTextFilePicker}
        />
        <span className="ml-auto hidden shrink-0 type-caption tabular-nums text-[var(--fg-3)] sm:inline">
          {draft.text.length} / 10000
        </span>
      </div>
    </div>
  );
}

function AgentToolToggle({
  icon,
  label,
  checked,
  disabled,
  ariaLabel,
  unavailable = false,
  unavailableLabel = "不可用",
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  checked: boolean;
  disabled: boolean;
  ariaLabel?: string;
  unavailable?: boolean;
  unavailableLabel?: string;
  onClick: () => void;
}) {
  const stateLabel = unavailable
    ? unavailableLabel
    : checked
      ? "已开启"
      : "已关闭";
  return (
    <Button
      variant={checked ? "secondary" : "ghost"}
      size="sm"
      onClick={onClick}
      disabled={disabled}
      aria-pressed={checked}
      aria-label={ariaLabel ?? `${label}${stateLabel}`}
      className={cn(
        "h-8 shrink-0 gap-1 px-2 type-caption max-sm:min-h-11",
        checked &&
          "border-accent-border bg-accent-soft text-accent shadow-[var(--shadow-amber)]",
      )}
      leftIcon={icon}
    >
      <span>{label}</span>
      <span className="text-[var(--fg-3)]">{stateLabel}</span>
    </Button>
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
    <div className="flex min-h-10 items-center gap-2 border-t border-danger-border bg-danger-soft px-3 py-2 type-caption text-[var(--danger-fg)]">
      <span role="alert" className="min-w-0 flex-1">{error}</span>
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
