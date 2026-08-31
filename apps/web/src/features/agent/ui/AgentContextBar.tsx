"use client";

import { Bot, Images, Loader2, Radio, Settings2, Trash2 } from "lucide-react";
import { useRef, useState } from "react";
import { DesktopPopover } from "@/components/ui/composer/desktop/DesktopPopover";
import { Button, IconButton, Select, Switch } from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile";
import { cn } from "@/lib/utils";
import type { AgentRealtimeStatus, } from "@/store/agent/useAgentStore";
import type {
  AgentRun,
  AgentSession,
  AgentSessionImageList,
  AgentSessionPatchInput,
} from "../model/contracts";

export interface AgentPromptOption {
  id: string;
  name: string;
}

export function AgentContextBar({
  platform,
  session,
  realtimeStatus,
  activeRun,
  toolGatewayConfigured,
  prompts,
  saving,
  onPatch,
  images,
  imagesLoading,
  removingImageId,
  onEjectImage,
}: {
  platform: "desktop" | "mobile";
  session: AgentSession | null;
  realtimeStatus: AgentRealtimeStatus;
  activeRun: AgentRun | null;
  toolGatewayConfigured: boolean;
  prompts: AgentPromptOption[];
  saving: boolean;
  onPatch: (patch: AgentSessionPatchInput) => void;
  images: AgentSessionImageList | null;
  imagesLoading: boolean;
  removingImageId: string | null;
  onEjectImage: (imageId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const anchorRef = useRef<HTMLDivElement | null>(null);
  const connectionLabel = agentConnectionLabel(realtimeStatus);
  const settings = session ? (
    <AgentContextSettings
      key={session.id}
      session={session}
      prompts={prompts}
      saving={saving}
      onPatch={onPatch}
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
          "flex min-h-11 shrink-0 items-center gap-2 border-b border-[var(--border-subtle)] bg-[var(--surface-chrome)]/88 px-3",
          platform === "desktop" ? "h-11 md:px-4" : "overflow-x-auto no-scrollbar",
        )}
      >
        <Bot className="h-4 w-4 shrink-0 text-accent" aria-hidden />
        <p className="min-w-0 flex-1 truncate type-nav text-[var(--fg-0)]">
          {session?.title || "新会话"}
        </p>
        {activeRun ? (
          <span
            role="status"
            className="inline-flex min-h-8 shrink-0 items-center gap-1.5 rounded-full border border-accent-border bg-accent-soft px-2 type-caption text-[var(--fg-1)]"
          >
            <Loader2 className="h-3.5 w-3.5 animate-spin text-accent" aria-hidden />
            {agentRunPhaseLabel(activeRun)}
          </span>
        ) : null}
        <span
          role="status"
          className="inline-flex min-h-8 shrink-0 items-center gap-1.5 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-1)] px-2 type-caption text-[var(--fg-2)]"
        >
          <Radio
            className={cn(
              "h-3.5 w-3.5",
              realtimeStatus === "open" ? "text-success" : "text-warning",
            )}
            aria-hidden
          />
          {connectionLabel}
        </span>
        {!toolGatewayConfigured ? (
          <span className="hidden type-caption text-warning sm:inline">生图未就绪</span>
        ) : null}
        <div ref={anchorRef}>
          <IconButton
            size="sm"
            variant={open ? "secondary" : "ghost"}
            onClick={() => setOpen((value) => !value)}
            aria-label="Agent 会话设置"
            aria-expanded={open}
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
          ariaLabel="Agent 会话设置"
          className="w-[360px] p-0"
        >
          {settings}
        </DesktopPopover>
      ) : (
        <BottomSheet
          open={open}
          onClose={() => setOpen(false)}
          ariaLabel="Agent 会话设置"
          snapPoints={["70%"]}
        >
          {settings}
        </BottomSheet>
      )}
    </>
  );
}

function agentRunPhaseLabel(run: AgentRun): string {
  if (run.status === "queued") return "排队中";
  if (run.cancel_requested_at) return "停止中";
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
  if (status === "open") return "实时";
  if (status === "connecting") return "连接中";
  return "轮询";
}

function AgentContextSettings({
  session,
  prompts,
  saving,
  onPatch,
  images,
  imagesLoading,
  removingImageId,
  onEjectImage,
}: {
  session: AgentSession;
  prompts: AgentPromptOption[];
  saving: boolean;
  onPatch: (patch: AgentSessionPatchInput) => void;
  images: AgentSessionImageList | null;
  imagesLoading: boolean;
  removingImageId: string | null;
  onEjectImage: (imageId: string) => void;
}) {
  const [systemPrompt, setSystemPrompt] = useState(session.default_system ?? "");
  return (
    <div className="grid gap-4 p-4">
      <div className="flex min-h-11 items-center justify-between gap-4 border-b border-[var(--border-subtle)] pb-3">
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
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
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
