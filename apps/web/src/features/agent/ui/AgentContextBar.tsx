"use client";

import {
  Bot,
  GitBranch,
  Images,
  Loader2,
  Pencil,
  Pin,
  Settings2,
  Trash2,
} from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { DesktopPopover } from "@/components/ui/composer/desktop/DesktopPopover";
import {
  Button,
  IconButton,
  Input,
  Select,
  Switch,
  Tooltip,
} from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile";
import { cn } from "@/lib/utils";
import type { AgentRealtimeStatus } from "@/store/agent/useAgentStore";
import type {
  AgentDraft,
  AgentImageDefaults,
  AgentModelOption,
  AgentRun,
  AgentSession,
  AgentSessionImageList,
  AgentSessionPatchInput,
} from "../model/contracts";
import { AgentQuickSettings } from "./AgentComposerSettings";

export interface AgentPromptOption {
  id: string;
  name: string;
}

export function AgentContextBar({
  platform,
  session,
  realtimeStatus,
  operationLabel = null,
  activeRun,
  toolGatewayConfigured,
  defaultModel,
  modelOptions,
  draft,
  prompts,
  saving,
  branching,
  onBranch,
  onPatch,
  onDraftChange,
  onDefaultsChange,
  images,
  imagesLoading,
  removingImageId,
  onEjectImage,
}: {
  platform: "desktop" | "mobile";
  session: AgentSession | null;
  realtimeStatus: AgentRealtimeStatus;
  operationLabel?: string | null;
  activeRun: AgentRun | null;
  toolGatewayConfigured: boolean;
  defaultModel: string | null;
  modelOptions: AgentModelOption[];
  draft: AgentDraft;
  prompts: AgentPromptOption[];
  saving: boolean;
  branching: boolean;
  onBranch: () => void;
  onPatch: (patch: AgentSessionPatchInput) => void;
  onDraftChange: (patch: Partial<AgentDraft>) => void;
  onDefaultsChange: (patch: Partial<AgentImageDefaults>) => void;
  images: AgentSessionImageList | null;
  imagesLoading: boolean;
  removingImageId: string | null;
  onEjectImage: (imageId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const status = agentContextStatus(
    realtimeStatus,
    activeRun,
    toolGatewayConfigured,
  );

  const settings = session ? (
    <AgentContextSettings
      key={session.id}
      session={session}
      draft={draft}
      defaultModel={defaultModel}
      modelOptions={modelOptions}
      toolGatewayConfigured={toolGatewayConfigured}
      prompts={prompts}
      saving={saving}
      onPatch={onPatch}
      onDraftChange={onDraftChange}
      onDefaultsChange={onDefaultsChange}
      images={images}
      imagesLoading={imagesLoading}
      removingImageId={removingImageId}
      onEjectImage={onEjectImage}
    />
  ) : (
    <p className="p-4 type-body-sm text-[var(--fg-2)]">新建会话后可设置</p>
  );

  return (
    <>
      <div
        className={cn(
          "flex min-h-11 shrink-0 items-center gap-1.5 border-b border-[var(--border-subtle)] bg-[var(--surface-chrome)]/88 px-2 sm:gap-2 sm:px-3",
          platform === "desktop" && "h-11 md:px-4",
        )}
      >
        <Bot className="hidden h-4 w-4 shrink-0 text-accent sm:block" aria-hidden />
        <AgentSessionTitle
          key={session?.id ?? "new"}
          session={session}
          saving={saving}
          onPatch={onPatch}
        />
        <AgentContextIndicator status={operationLabel ? { label: operationLabel, detail: operationLabel, tone: "limited" } : status} />
        <AgentPinButton
          session={session}
          saving={saving}
          onPatch={onPatch}
        />
        <IconButton
          size="sm"
          variant="ghost"
          onClick={onBranch}
          disabled={!session || saving || branching || Boolean(activeRun)}
          loading={branching}
          aria-label="分支会话"
          tooltip={activeRun ? "运行结束后可分支" : "分支会话"}
          tooltipSide="bottom"
        >
          <GitBranch className="h-4 w-4" aria-hidden />
        </IconButton>
        <div ref={anchorRef}>
          <IconButton
            size="sm"
            variant={open ? "secondary" : "ghost"}
            onClick={() => setOpen((value) => !value)}
            aria-label="Agent 参数与会话设置"
            aria-expanded={open}
            tooltip="会话设置"
            tooltipSide="bottom"
          >
            <Settings2 className="h-4 w-4" aria-hidden />
          </IconButton>
        </div>
      </div>

      {platform === "desktop" ? (
        <DesktopPopover
          open={open}
          onClose={() => setOpen(false)}
          anchorRef={anchorRef}
          align="right"
          ariaLabel="Agent 参数与会话设置"
          className="w-[360px] p-0"
        >
          {settings}
        </DesktopPopover>
      ) : (
        <BottomSheet
          open={open}
          onClose={() => setOpen(false)}
          ariaLabel="Agent 参数与会话设置"
          snapPoints={["70%"]}
        >
          {settings}
        </BottomSheet>
      )}
    </>
  );
}

function AgentSessionTitle({
  session,
  saving,
  onPatch,
}: {
  session: AgentSession | null;
  saving: boolean;
  onPatch: (patch: AgentSessionPatchInput) => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState("");
  const renameTriggerRef = useRef<HTMLButtonElement | null>(null);
  const finishingRef = useRef(false);
  const displayTitle = session?.title || "新会话";
  const finishRename = useCallback(
    (commit: boolean) => {
      if (finishingRef.current) return;
      finishingRef.current = true;
      const normalized = title.trim() || "新会话";
      setRenaming(false);
      if (commit && session && normalized !== session.title) {
        onPatch({ title: normalized });
      }
      window.requestAnimationFrame(() => {
        finishingRef.current = false;
        renameTriggerRef.current?.focus({ preventScroll: true });
      });
    },
    [onPatch, session, title],
  );

  if (renaming && session) {
    return (
      <form
        className="min-w-0 flex-1"
        onSubmit={(event) => {
          event.preventDefault();
          finishRename(true);
        }}
      >
        <Input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          onBlur={() => finishRename(true)}
          onKeyDown={(event) => {
            if (event.key !== "Escape" || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
            event.preventDefault();
            finishRename(false);
          }}
          disabled={saving}
          aria-label="会话名称"
          autoFocus
          maxLength={255}
          className="h-8 min-h-8"
        />
      </form>
    );
  }

  return (
    <Button
      ref={renameTriggerRef}
      variant="ghost"
      size="sm"
      onClick={() => {
        if (!session || saving) return;
        finishingRef.current = false;
        setTitle(displayTitle);
        setRenaming(true);
      }}
      disabled={!session}
      aria-disabled={saving || undefined}
      aria-label={`重命名会话：${displayTitle}`}
      className="h-9 min-w-0 flex-1 justify-start px-1 max-sm:min-h-11"
      rightIcon={<Pencil className="h-3.5 w-3.5 opacity-60" aria-hidden />}
    >
      <span className="truncate type-nav text-[var(--fg-0)]">
        {displayTitle}
      </span>
    </Button>
  );
}

type AgentContextStatus = ReturnType<typeof agentContextStatus>;

const STATUS_TONE_CLASS: Record<AgentContextStatus["tone"], string> = {
  active: "border-accent-border bg-accent-soft !text-[var(--fg-0)]",
  ready: "border-success-border bg-success-soft !text-[var(--fg-1)]",
  limited: "border-warning-border bg-warning-soft !text-[var(--warning-fg)]",
};

const STATUS_DOT_CLASS: Record<AgentContextStatus["tone"], string> = {
  active: "animate-pulse bg-accent motion-reduce:animate-none",
  ready: "bg-success",
  limited: "bg-warning",
};

function AgentContextIndicator({ status }: { status: AgentContextStatus }) {
  return (
    <Tooltip content={status.detail} side="bottom">
      <span
        role="status"
        aria-live="polite"
        aria-label={status.detail}
        tabIndex={0}
        className={cn(
          "inline-flex min-h-8 min-w-0 shrink-0 items-center justify-center gap-1.5 rounded-full border px-2 type-caption focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
          STATUS_TONE_CLASS[status.tone],
        )}
      >
        <span className="relative flex h-2 w-2" aria-hidden>
          {status.tone === "active" ? (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75 motion-reduce:animate-none" />
          ) : null}
          <span
            className={cn(
              "relative inline-flex h-2 w-2 rounded-full",
              STATUS_DOT_CLASS[status.tone],
            )}
          />
        </span>
        <span className="max-w-20 truncate">{status.label}</span>
      </span>
    </Tooltip>
  );
}

function AgentPinButton({
  session,
  saving,
  onPatch,
}: {
  session: AgentSession | null;
  saving: boolean;
  onPatch: (patch: AgentSessionPatchInput) => void;
}) {
  const pinned = session?.pinned ?? false;
  return (
    <IconButton
      size="sm"
      variant={pinned ? "secondary" : "ghost"}
      onClick={() => session && onPatch({ pinned: !pinned })}
      disabled={!session || saving}
      aria-label={pinned ? "取消置顶会话" : "置顶会话"}
      aria-pressed={pinned}
      tooltip={pinned ? "取消置顶" : "置顶会话"}
      tooltipSide="bottom"
      className={pinned ? "border-accent-border text-accent" : undefined}
    >
      <Pin
        className="h-4 w-4"
        fill={pinned ? "currentColor" : "none"}
        aria-hidden
      />
    </IconButton>
  );
}

function agentRunPhaseLabel(run: AgentRun): string {
  if (run.cancel_requested_at) return "停止待确认";
  if (run.id.startsWith("optimistic:")) return "提交中";
  if (run.status === "queued") return "排队中";
  const activeTool = run.tool_calls.find(
    (tool) => tool.status === "queued" || tool.status === "running",
  );
  if (activeTool?.name === "lumen_web_search") return "搜索中";
  if (activeTool?.name?.startsWith("lumen_") && activeTool.name.includes("file")) {
    return "读取文件";
  }
  if (activeTool) return "调用工具";
  return run.turn_count > 0 ? "生成回复" : "思考中";
}

function agentConnectionLabel(status: AgentRealtimeStatus): string {
  if (status === "open") return "实时连接";
  if (status === "connecting") return "连接中";
  return "轮询同步";
}

function agentContextStatus(
  realtimeStatus: AgentRealtimeStatus,
  activeRun: AgentRun | null,
  toolGatewayConfigured: boolean,
): {
  label: string;
  detail: string;
  tone: "active" | "ready" | "limited";
} {
  const connection = agentConnectionLabel(realtimeStatus);
  const imageState = toolGatewayConfigured ? "生图可用" : "生图不可用";
  if (activeRun) {
    const phase = agentRunPhaseLabel(activeRun);
    return {
      label: phase,
      detail: `${phase}；${connection}；${imageState}`,
      tone: "active",
    };
  }
  if (realtimeStatus === "open" && toolGatewayConfigured) {
    return {
      label: "已就绪",
      detail: `${connection}；${imageState}`,
      tone: "ready",
    };
  }
  return {
    label: toolGatewayConfigured ? connection : "能力受限",
    detail: `${connection}；${imageState}`,
    tone: "limited",
  };
}

function AgentContextSettings({
  session,
  draft,
  defaultModel,
  modelOptions,
  toolGatewayConfigured,
  prompts,
  saving,
  onPatch,
  onDraftChange,
  onDefaultsChange,
  images,
  imagesLoading,
  removingImageId,
  onEjectImage,
}: {
  session: AgentSession;
  draft: AgentDraft;
  defaultModel: string | null;
  modelOptions: AgentModelOption[];
  toolGatewayConfigured: boolean;
  prompts: AgentPromptOption[];
  saving: boolean;
  onPatch: (patch: AgentSessionPatchInput) => void;
  onDraftChange: (patch: Partial<AgentDraft>) => void;
  onDefaultsChange: (patch: Partial<AgentImageDefaults>) => void;
  images: AgentSessionImageList | null;
  imagesLoading: boolean;
  removingImageId: string | null;
  onEjectImage: (imageId: string) => void;
}) {
  const [systemPrompt, setSystemPrompt] = useState(session.default_system ?? "");
  return (
    <div className="mobile-dialog-scroll grid min-h-0 gap-4 overflow-y-auto p-4">
      <AgentQuickSettings
        draft={draft}
        disabled={saving}
        defaultModel={defaultModel}
        modelOptions={modelOptions}
        imageGenerationAvailable={toolGatewayConfigured}
        onModelChange={(model) => onDraftChange({ model })}
        onReasoningEffortChange={(reasoningEffort) =>
          onDraftChange({ reasoningEffort })
        }
        onDefaultsChange={onDefaultsChange}
      />
      <div className="flex min-h-11 items-center justify-between gap-4 border-b border-[var(--border-subtle)] pt-1 pb-3">
        <div>
          <p className="type-label text-[var(--fg-0)]">会话记忆</p>
          <p className="type-caption">使用账号记忆和会话摘要</p>
        </div>
        <Switch
          checked={!session.memory_disabled}
          onCheckedChange={(enabled) => onPatch({ memory_disabled: !enabled })}
          disabled={saving}
          aria-label="使用会话记忆"
        />
      </div>
      <label className="grid gap-1.5 type-caption text-[var(--fg-2)]">
        系统提示词
        <Select
          value={session.default_system_prompt_id ?? ""}
          onChange={(event) =>
            onPatch({ default_system_prompt_id: event.target.value || null })
          }
          disabled={saving}
          aria-label="会话系统提示词"
        >
          <option value="">账号默认</option>
          {prompts.map((prompt) => (
            <option key={prompt.id} value={prompt.id}>{prompt.name}</option>
          ))}
        </Select>
      </label>
      <label className="grid gap-1.5 type-caption text-[var(--fg-2)]">
        会话指令
        <textarea
          value={systemPrompt}
          onChange={(event) => setSystemPrompt(event.target.value)}
          maxLength={10_000}
          rows={5}
          disabled={saving}
          className="control-shell min-h-28 resize-y px-3 py-2 type-body-sm text-[var(--fg-0)] outline-none placeholder:text-[var(--fg-2)] focus:border-accent-border"
          placeholder="可选"
        />
      </label>
      <div className="grid gap-2 border-t border-[var(--border-subtle)] pt-3">
        <div className="flex min-h-9 items-center gap-2">
          <Images className="h-4 w-4 text-accent" aria-hidden />
          <p className="type-label text-[var(--fg-0)]">会话图片</p>
          <span className="ml-auto type-caption text-[var(--fg-2)]">
            {images?.used ?? 0}/{images?.maximum ?? 64}
          </span>
        </div>
        {imagesLoading ? (
          <div className="flex min-h-10 items-center gap-2 type-caption text-[var(--fg-2)]">
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden />
            正在载入
          </div>
        ) : null}
        {!imagesLoading && images?.items.some((item) => item.active) ? (
          <div className="max-h-44 overflow-y-auto border-y border-[var(--border-subtle)]">
            {images.items.filter((item) => item.active).map((item) => (
              <div
                key={item.image_id}
                className="flex min-h-11 items-center gap-2 border-b border-[var(--border-subtle)] px-1 last:border-b-0"
              >
                <span className="w-12 shrink-0 type-caption font-medium text-[var(--fg-0)]">
                  {item.reference_label}
                </span>
                <span className="min-w-0 flex-1 truncate type-caption text-[var(--fg-2)]">
                  {item.display_label || item.role}
                </span>
                <IconButton
                  size="sm"
                  variant="ghost"
                  onClick={() => onEjectImage(item.image_id)}
                  disabled={saving || removingImageId !== null}
                  loading={removingImageId === item.image_id}
                  aria-label={`移除 ${item.reference_label}`}
                  tooltip="移出会话图片"
                >
                  <Trash2 className="h-4 w-4" aria-hidden />
                </IconButton>
              </div>
            ))}
          </div>
        ) : null}
        {!imagesLoading && !images?.items.some((item) => item.active) ? (
          <p className="type-caption text-[var(--fg-2)]">暂无会话图片</p>
        ) : null}
      </div>
      <Button
        variant="primary"
        loading={saving}
        onClick={() => onPatch({ default_system: systemPrompt || null })}
      >
        保存
      </Button>
    </div>
  );
}
