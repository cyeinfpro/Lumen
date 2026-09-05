import type { AgentDraft, AgentMessage, AgentRun } from "../model/contracts";

export function agentDraftSummary(draft: AgentDraft, imageGenerationAvailable: boolean): string {
  const tools: string[] = [];
  if (draft.attachments.length > 0) tools.push(`本轮输入 ${draft.attachments.length} 张`);
  if (draft.allowWebSearch) tools.push("联网");
  if (draft.files.length > 0) tools.push(`文件 ${draft.files.length}`);
  tools.push(draft.allowImage && imageGenerationAvailable
    ? `${draft.imageDefaults.count} 张 · ${draft.imageDefaults.aspect_ratio} · ${draft.imageDefaults.quality.toUpperCase()}`
    : "仅文本");
  if (draft.allowImage && !imageGenerationAvailable) tools.push("生图不可用");
  return tools.join(" · ");
}

export function isAgentSubmissionUncertain(run: AgentRun): boolean {
  // The requests lane preserves an unconfirmed local attempt using this exact code.
  return run.error_code === "agent_submission_uncertain";
}

export function hasAgentSubmissionUncertain(
  messages: AgentMessage[],
  runsById: Record<string, AgentRun>,
): boolean {
  return messages.some((message) => {
    if (message.role !== "assistant" || !message.agentRunId) return false;
    const run = runsById[message.agentRunId];
    return Boolean(run && isAgentSubmissionUncertain(run));
  });
}

export function agentRunPresentation(run: AgentRun): {
  kind: "uncertain" | "submitting" | "stopping" | AgentRun["status"];
  label: string;
} {
  if (isAgentSubmissionUncertain(run)) return { kind: "uncertain", label: "提交待确认" };
  const active = run.status === "queued" || run.status === "running";
  if (active && run.cancel_requested_at) return { kind: "stopping", label: "停止请求已提交，等待确认" };
  if (active && run.id.startsWith("optimistic:")) return { kind: "submitting", label: "提交中" };
  const labels: Record<AgentRun["status"], string> = {
    queued: "等待运行", running: "Agent 运行中", succeeded: "运行完成",
    partial: "部分完成", failed: "运行失败", cancelled: "已取消",
  };
  return { kind: run.status, label: labels[run.status] };
}

export function currentAgentOperationLabel(input: {
  submitting: boolean;
  creating: boolean;
  stopping: boolean;
  messages: AgentMessage[];
  runsById: Record<string, AgentRun>;
}): string | null {
  if (input.submitting || input.creating) return "提交中";
  if (input.stopping) return "停止请求中";
  if (hasAgentSubmissionUncertain(input.messages, input.runsById)) return "提交待确认";
  return null;
}

export function agentEstimateLabel(label: string | null): string | null {
  // Keep the billing formatter's units/precision; this is an estimate, not a debit.
  return label?.replace(/^预计扣 /u, "预计 ") ?? null;
}
