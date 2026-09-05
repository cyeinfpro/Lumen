"use client";

import { useState } from "react";
import {
  ArrowDownRight,
  Check,
  ChevronDown,
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
import type {
  AgentFileToolDetails,
  AgentImageToolDetails,
  AgentToolCall as AgentToolCallContract,
  AgentToolDetails,
  AgentWebSearchToolDetails,
} from "../model/contracts";

const TOOL_STATUS: Record<AgentToolCallContract["status"], string> = {
  queued: "等待执行",
  running: "执行中",
  succeeded: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
  timed_out: "执行超时",
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

function durationMilliseconds(tool: AgentToolCallContract): number | null {
  if (tool.duration_ms !== null) return tool.duration_ms;
  if (!tool.started_at) return null;
  const start = Date.parse(tool.started_at);
  const end = tool.finished_at ? Date.parse(tool.finished_at) : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return end - start;
}

function formatDuration(milliseconds: number | null): string | null {
  if (milliseconds === null) return null;
  if (milliseconds < 100) return "< 0.1 秒";
  if (milliseconds < 1_000) return `${milliseconds} 毫秒`;
  const seconds = milliseconds / 1_000;
  return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} 秒`;
}

function toolSummary(
  tool: AgentToolCallContract,
  details: AgentToolDetails | null,
): string {
  const status = toolStatusText(tool);
  if (!details) return status;
  if (details.kind === "web_search" && details.query) {
    return `${status} · ${details.query}`;
  }
  if (
    (details.kind === "file_read" ||
      details.kind === "file_list" ||
      details.kind === "file_search") &&
    details.file_names.length > 0
  ) {
    return `${status} · ${details.file_names.join("、")}`;
  }
  if (details.kind === "image") {
    const count = details.count ?? tool.generation_count;
    return count > 0 ? `${status} · ${count} 张` : status;
  }
  return status;
}

function ToolStatusIndicator({
  status,
  active,
  failed,
}: {
  status: AgentToolCallContract["status"];
  active: boolean;
  failed: boolean;
}) {
  if (active) {
    return (
      <Loader2
        className="h-4 w-4 animate-spin text-accent motion-reduce:animate-none"
        aria-hidden
      />
    );
  }
  if (status === "succeeded") {
    return <Check className="h-4 w-4 text-success" aria-hidden />;
  }
  if (failed) {
    return (
      <TriangleAlert
        className="h-4 w-4 text-[var(--danger-fg)]"
        aria-hidden
      />
    );
  }
  return <X className="h-4 w-4 text-[var(--fg-2)]" aria-hidden />;
}

function toolRowTone(failed: boolean, uncertain: boolean) {
  // The row owns role="status"; the persistent ToolError owns role="alert".
  if (failed) return { border: "border-danger-border", text: "text-[var(--danger-fg)]" };
  if (uncertain) return { border: "border-warning-border", text: "text-[var(--warning-fg)]" };
  return { border: "border-[var(--border-subtle)]", text: "text-[var(--fg-2)]" };
}

export function AgentToolCall({ tool, artifactId }: { tool: AgentToolCallContract; artifactId?: string }) {
  const [expanded, setExpanded] = useState(false);
  const active = tool.status === "queued" || tool.status === "running";
  const uncertain = (tool.status === "failed" || tool.status === "timed_out") && tool.error_code === "agent_tool_result_unknown";
  const failed = !uncertain && (tool.status === "failed" || tool.status === "timed_out");
  const tone = toolRowTone(failed, uncertain);
  const presentation = toolPresentation(tool);
  const duration = formatDuration(durationMilliseconds(tool));
  const detailsId = `agent-tool-details-${tool.id}`;
  const summary = toolSummary(tool, tool.details);

  return (
    <div
      data-agent-tool-call={tool.id}
      className={cn(
        "min-w-0 border-b border-l-2 type-caption",
        tone.border,
      )}
    >
      <span role="status" aria-live="polite" className="sr-only">
        {presentation.label}，{toolStatusText(tool)}
      </span>
      <button
        type="button"
        onClick={() => setExpanded((previous) => !previous)}
        aria-expanded={expanded}
        aria-controls={detailsId}
        className="flex min-h-11 w-full items-center gap-2.5 px-3 py-2 text-left focus-visible:outline-none focus-visible:shadow-[var(--ring)]"
      >
        <span className="flex h-7 w-7 shrink-0 items-center justify-center">
          <presentation.Icon className="h-4 w-4 text-[var(--fg-1)]" aria-hidden />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block type-label text-[var(--fg-0)]">
            {presentation.label}
          </span>
          <span
            className={cn(
              "block break-words [overflow-wrap:anywhere]",
              tone.text,
            )}
          >
            {summary}
            {duration ? ` · 耗时 ${duration}` : ""}
          </span>
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          {uncertain ? <TriangleAlert className="h-4 w-4 text-[var(--warning-fg)]" aria-hidden /> : <ToolStatusIndicator status={tool.status} active={active} failed={failed} />}
          <ChevronDown
            className={cn(
              "h-4 w-4 text-[var(--fg-2)] transition-transform duration-[var(--dur-collapse)] ease-[var(--ease-develop)] motion-reduce:transition-none",
              expanded && "rotate-180",
            )}
            aria-hidden
          />
        </span>
      </button>
      <div
        data-agent-tool-disclosure
        className={cn(
          "grid transition-[grid-template-rows] duration-[var(--dur-collapse)] ease-[var(--ease-develop)] motion-reduce:transition-none",
          expanded ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
        )}
      >
        <div className="min-h-0 overflow-hidden">
          <div
            id={detailsId}
            role="region"
            aria-label={`${presentation.label}执行详情`}
            aria-hidden={!expanded}
            inert={!expanded}
            className="space-y-2 px-3.5 pb-3 text-[var(--fg-2)]"
          >
            <ToolDetails details={tool.details} duration={duration} />
          </div>
        </div>
      </div>
      {failed ? <ToolError tool={tool} /> : null}
      {artifactId ? (
        <a
          href={`#agent-generation-${artifactId}`}
          className="mx-3 mb-2 inline-flex min-h-9 items-center gap-1 type-caption text-[var(--fg-1)] hover:text-[var(--fg-0)] max-sm:min-h-11"
        >
          <ArrowDownRight className="h-4 w-4" aria-hidden />
          查看产物 · {tool.generation_ids.length}
        </a>
      ) : null}
    </div>
  );
}

function ToolDetails({
  details,
  duration,
}: {
  details: AgentToolDetails | null;
  duration: string | null;
}) {
  return (
    <>
      {details?.kind === "web_search" ? (
        <WebSearchDetails details={details} />
      ) : null}
      {details &&
      (details.kind === "file_list" ||
        details.kind === "file_read" ||
        details.kind === "file_search") ? (
        <FileToolDetails details={details} />
      ) : null}
      {details?.kind === "image" ? (
        <ImageToolDetails details={details} />
      ) : null}
      {!details ? (
        <p className="type-caption text-[var(--fg-2)]">
          此工具没有可公开的参数详情。
        </p>
      ) : null}
      {duration ? <DetailRow label="耗时" value={duration} /> : null}
    </>
  );
}

function WebSearchDetails({
  details,
}: {
  details: AgentWebSearchToolDetails;
}) {
  return (
    <>
      {details.query ? <DetailText label="查询词" value={details.query} /> : null}
      <SnippetList label="结果摘要" snippets={details.result_snippets} />
    </>
  );
}

function FileToolDetails({ details }: { details: AgentFileToolDetails }) {
  const lines =
    details.line_start && details.line_end
      ? `${details.line_start}-${details.line_end}`
      : details.line_start
        ? `从 ${details.line_start} 行开始`
        : null;
  return (
    <>
      {details.file_names.length > 0 ? (
        <DetailText label="文件" value={details.file_names.join("、")} mono />
      ) : null}
      {details.query ? <DetailText label="搜索词" value={details.query} /> : null}
      {lines ? <DetailRow label="行范围" value={lines} /> : null}
      <SnippetList label="结果摘要" snippets={details.result_snippets} mono />
    </>
  );
}

function ImageToolDetails({ details }: { details: AgentImageToolDetails }) {
  const parameters = [
    details.count ? `${details.count} 张` : null,
    details.aspect_ratio,
    details.quality?.toUpperCase(),
    renderQualityLabel(details.render_quality),
    backgroundLabel(details.background),
    details.output_format?.toUpperCase(),
    details.reference_count > 0 ? `${details.reference_count} 张参考图` : null,
  ].filter((value): value is string => Boolean(value));
  return (
    <>
      {details.prompt ? <DetailText label="图片 Prompt" value={details.prompt} /> : null}
      {parameters.length > 0 ? (
        <DetailRow label="参数" value={parameters.join(" · ")} />
      ) : null}
    </>
  );
}

function DetailText({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <p className="type-caption text-[var(--fg-2)]">{label}</p>
      <p
        className={cn(
          "mt-1 whitespace-pre-wrap break-words type-caption text-[var(--fg-0)] [overflow-wrap:anywhere]",
          mono && "font-mono",
        )}
      >
        {value}
      </p>
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <span>{label}</span>
      <span className="break-words text-right tabular-nums text-[var(--fg-1)]">
        {value}
      </span>
    </div>
  );
}

function SnippetList({
  label,
  snippets,
  mono = false,
}: {
  label: string;
  snippets: string[];
  mono?: boolean;
}) {
  if (snippets.length === 0) return null;
  return (
    <div>
      <p className="type-caption text-[var(--fg-2)]">{label}</p>
      <ul className="mt-1 space-y-1.5">
        {snippets.map((snippet, index) => (
          <li
            key={`${index}:${snippet}`}
            className={cn(
              "break-words border-l border-[var(--border-subtle)] pl-2.5 type-caption text-[var(--fg-1)] [overflow-wrap:anywhere]",
              mono && "font-mono",
            )}
          >
            {snippet}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ToolError({ tool }: { tool: AgentToolCallContract }) {
  return (
    <div
      role="alert"
      className="px-3 pb-2 text-[var(--danger-fg)]"
    >
      <span className="block type-label">错误</span>
      <span className="block break-words type-caption [overflow-wrap:anywhere]">
        {tool.error_code
          ? TOOL_ERRORS[tool.error_code] || "工具执行失败"
          : "工具执行失败"}
      </span>
    </div>
  );
}

function renderQualityLabel(
  value: AgentImageToolDetails["render_quality"],
): string | null {
  if (value === null) return null;
  return {
    auto: "自动渲染",
    low: "草稿",
    medium: "标准",
    high: "精细",
  }[value];
}

function backgroundLabel(
  value: AgentImageToolDetails["background"],
): string | null {
  if (value === null) return null;
  return {
    auto: "自动背景",
    opaque: "不透明背景",
    transparent: "透明背景",
  }[value];
}
