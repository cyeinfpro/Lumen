"use client";

import {
  Check,
  FileSearch,
  FileText,
  Files,
  Globe2,
  ImageIcon,
  Loader2,
  TriangleAlert,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { AgentToolCall as AgentToolCallContract } from "../model/contracts";

const TOOL_STATUS: Record<AgentToolCallContract["status"], string> = {
  queued: "等待执行",
  running: "执行中",
  succeeded: "已完成",
  failed: "提交失败",
  cancelled: "已取消",
  timed_out: "提交超时",
};

const TOOL_ERRORS: Record<string, string> = {
  agent_image_provider_unavailable: "图片供应商不可用",
  agent_reference_not_allowed: "参考图不在当前会话中",
  agent_reference_not_found: "参考图已不可用",
  agent_session_reference_limit_reached: "会话图片已达上限",
  agent_tool_limit_reached: "本轮工具调用已达上限",
  agent_image_limit_reached: "本轮生成数量已达上限",
  agent_tool_result_unknown: "工具结果仍待确认",
  agent_web_search_limit_reached: "本轮联网搜索已达上限",
  agent_web_search_unavailable: "联网搜索暂不可用",
  agent_file_tool_limit_reached: "本轮文件工具已达上限",
  agent_file_not_found: "文件已不可用",
  INSUFFICIENT_BALANCE: "余额不足",
  NO_ACTIVE_API_KEY: "API 密钥不可用",
};

function toolStatusText(tool: AgentToolCallContract): string {
  if (
    (tool.status === "failed" || tool.status === "timed_out") &&
    tool.error_code
  ) {
    return TOOL_ERRORS[tool.error_code] ?? "工具执行失败";
  }
  return TOOL_STATUS[tool.status];
}

function toolPresentation(tool: AgentToolCallContract) {
  if (tool.name === "lumen_web_search") {
    return { label: "联网搜索", Icon: Globe2 };
  }
  if (tool.name === "lumen_list_files") {
    return { label: "查看文件", Icon: Files };
  }
  if (tool.name === "lumen_read_file") {
    return { label: "读取文件", Icon: FileText };
  }
  if (tool.name === "lumen_search_files") {
    return { label: "文件内搜索", Icon: FileSearch };
  }
  return {
    label: tool.mode === "image_to_image" ? "图生图" : "文生图",
    Icon: ImageIcon,
  };
}

export function AgentToolCall({ tool }: { tool: AgentToolCallContract }) {
  const active = tool.status === "queued" || tool.status === "running";
  const failed = tool.status === "failed" || tool.status === "timed_out";
  const presentation = toolPresentation(tool);
  const count = tool.generation_count || tool.generation_ids.length;
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex min-h-11 items-center gap-2 rounded-[var(--radius-card)] border px-3 py-2 type-caption",
        failed
          ? "border-danger-border bg-danger-soft"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)]",
      )}
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--bg-2)]">
        <presentation.Icon className="h-4 w-4 text-[var(--fg-1)]" aria-hidden />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block type-label text-[var(--fg-0)]">{presentation.label}</span>
        <span className={cn("block", failed ? "text-[var(--danger-fg)]" : "text-[var(--fg-2)]")}>
          {toolStatusText(tool)}
          {count > 0 ? ` · ${count} 个任务` : ""}
        </span>
      </span>
      {active ? (
        <Loader2 className="h-4 w-4 animate-spin text-accent" aria-hidden />
      ) : tool.status === "succeeded" ? (
        <Check className="h-4 w-4 text-success" aria-hidden />
      ) : failed ? (
        <TriangleAlert className="h-4 w-4 text-[var(--danger-fg)]" aria-hidden />
      ) : (
        <X className="h-4 w-4 text-[var(--fg-2)]" aria-hidden />
      )}
    </div>
  );
}
