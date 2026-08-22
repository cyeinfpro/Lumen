"use client";

import { Bot, Radio, Settings2 } from "lucide-react";
import { useRef, useState } from "react";
import { DesktopPopover } from "@/components/ui/composer/desktop/DesktopPopover";
import { Button, IconButton, Select, Switch } from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile";
import { cn } from "@/lib/utils";
import type { AgentRealtimeStatus, } from "@/store/agent/useAgentStore";
import type { AgentSession, AgentSessionPatchInput } from "../model/contracts";

export interface AgentPromptOption {
  id: string;
  name: string;
}

export function AgentContextBar({
  platform,
  session,
  realtimeStatus,
  toolGatewayConfigured,
  prompts,
  saving,
  onPatch,
}: {
  platform: "desktop" | "mobile";
  session: AgentSession | null;
  realtimeStatus: AgentRealtimeStatus;
  toolGatewayConfigured: boolean;
  prompts: AgentPromptOption[];
  saving: boolean;
  onPatch: (patch: AgentSessionPatchInput) => void;
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
}: {
  session: AgentSession;
  prompts: AgentPromptOption[];
  saving: boolean;
  onPatch: (patch: AgentSessionPatchInput) => void;
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
