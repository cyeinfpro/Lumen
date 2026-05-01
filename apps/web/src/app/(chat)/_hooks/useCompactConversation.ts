"use client";

// 手动压缩对话上下文的 React Query mutation hook（P0-3 前端入口）。
//
// 契约见 apiClient.ts 的 compactConversation：
//   POST /api/conversations/{conversationId}/compact
//   Body: { extra_instruction?: string, force?: boolean, background?: boolean }
//   200 命中压缩：    { status: "ok", compacted: true,  summary: CompactSummary }
//   200 未达阈值：    { status: "ok", compacted: false, reason: "below_budget", ... }
//   200 后台任务中：  { status: "pending", compacted: false, reason: "pending", job_id }
//   404 / 409 / 503: ApiError，详见 onError 注释
//
// 入口位置：
//   - 桌面：DesktopStudio 顶栏的 ContextWindowMeter（"压缩历史"按钮）
//   - 移动：MobileStudioTopBar 顶栏的 ContextWindowMeter（compact 模式 Archive 图标）
// 两端直接触发后台压缩，不再弹出手动压缩确认框。

import { useMutation, useQueryClient, type UseMutationOptions } from "@tanstack/react-query";

import {
  ApiError,
  compactConversation,
  type CompactConversationApiResponse,
  type CompactUnavailableReason,
} from "@/lib/apiClient";
import { qk } from "@/lib/queries";

export type {
  CompactConversationApiResponse,
  CompactConversationResponse,
  CompactSummary,
  CompactSummaryStatus,
  CompactUnavailableReason,
} from "@/lib/apiClient";

export interface CompactConversationVars {
  conversationId: string;
  extra_instruction?: string | null;
  // 默认 true：用户点"立即压缩"按钮就该真打上游，不管历史是否到阈值
  force?: boolean;
}

// 后端 503 payload 形如 { detail, reason }；其它错误码可能没有 reason。
function readUnavailableReason(err: unknown): CompactUnavailableReason | null {
  if (!(err instanceof ApiError)) return null;
  if (err.status !== 503) return null;
  const payload = err.payload;
  if (!payload || typeof payload !== "object") return null;
  const directReason = (payload as { reason?: unknown }).reason;
  const nestedError = (payload as { error?: unknown }).error;
  const nestedReason =
    nestedError && typeof nestedError === "object"
      ? (nestedError as { reason?: unknown }).reason
      : null;
  const reason = directReason ?? nestedReason;
  if (
    reason === "lock_busy" ||
    reason === "circuit_open" ||
    reason === "upstream_error"
  ) {
    return reason;
  }
  return null;
}

// 把后端错误翻译成"用户能读懂的一行话"，调用方可自行 toast。
export function describeCompactError(err: unknown): string {
  if (!(err instanceof ApiError)) {
    return err instanceof Error && err.message ? err.message : "压缩失败";
  }
  if (err.status === 404) return "对话不存在或无权限";
  if (err.status === 409) return "暂无可压缩的历史";
  if (err.status === 503) {
    const reason = readUnavailableReason(err);
    if (reason === "lock_busy") return "正在压缩中，稍后再试";
    if (reason === "circuit_open") return "压缩服务暂不可用";
    if (reason === "upstream_error") return "上游服务异常，稍后重试";
    return "压缩服务暂不可用";
  }
  if (err.status === 401) return "请重新登录";
  return err.message || `压缩失败 (HTTP ${err.status})`;
}

export function useCompactConversation(
  options?: Omit<
    UseMutationOptions<CompactConversationApiResponse, Error, CompactConversationVars>,
    "mutationFn"
  >,
) {
  const qc = useQueryClient();

  return useMutation<CompactConversationApiResponse, Error, CompactConversationVars>({
    mutationFn: async ({ conversationId, extra_instruction, force = true }) => {
      return compactConversation(conversationId, {
        extra_instruction,
        force,
        background: true,
      });
    },
    ...options,
    onSuccess: (data, vars, onMutateResult, ctx) => {
      if (data.status === "ok") {
        // 让下一次 chat 拿到最新摘要：会话上下文统计需主动 refetch（顶栏 token 计量器即时刷新），消息列表/会话列表交给 active observers 自动拉取。
        void qc.refetchQueries({
          queryKey: qk.conversationContext(vars.conversationId),
        });
        void qc.invalidateQueries({ queryKey: ["messages", vars.conversationId] });
        void qc.invalidateQueries({ queryKey: ["conversations"] });
      }
      options?.onSuccess?.(data, vars, onMutateResult, ctx);
    },
  });
}

// V1 hook 名兼容（部分组件已经引用了 useCompactConversationMutation 风格的导出名）。
export { useCompactConversation as useCompactConversationMutation };
