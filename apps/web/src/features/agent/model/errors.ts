import { ApiError } from "@/lib/api/errors";

export interface AgentErrorPresentation {
  title: string;
  detail: string;
  recoverable: boolean;
  href?: string;
  actionLabel?: string;
}

const ERROR_PRESENTATIONS: Record<string, AgentErrorPresentation> = {
  feature_disabled: {
    title: "Agent 未启用",
    detail: "管理员尚未开放 Agent。",
    recoverable: false,
  },
  agent_runtime_unavailable: {
    title: "Agent 服务异常",
    detail: "运行时暂不可用，稍后重试。",
    recoverable: true,
  },
  agent_run_timeout: {
    title: "运行达到时间上限",
    detail: "已保留当前结果，可以继续生成。",
    recoverable: true,
  },
  agent_output_truncated: {
    title: "输出达到长度限制",
    detail: "已保留当前结果，可从同一 Agent 会话继续。",
    recoverable: true,
  },
  agent_safety_budget_reached: {
    title: "运行达到安全上限",
    detail: "已保留当前文本和已接受的任务。",
    recoverable: true,
  },
  agent_runtime_shutdown: {
    title: "Agent 服务维护中",
    detail: "已保留当前结果，服务恢复后可继续。",
    recoverable: true,
  },
  agent_runtime_event_timeout: {
    title: "运行连接超时",
    detail: "已保留当前结果，可以继续生成。",
    recoverable: true,
  },
  agent_runtime_disconnected: {
    title: "运行连接中断",
    detail: "已保留当前结果，可以继续生成。",
    recoverable: true,
  },
  agent_provider_unavailable: {
    title: "对话通道不可用",
    detail: "没有可用的 Agent 对话供应商。",
    recoverable: true,
  },
  agent_image_provider_unavailable: {
    title: "生图通道不可用",
    detail: "图片供应商暂不可用，已保留文本结果。",
    recoverable: true,
  },
  agent_vision_model_unavailable: {
    title: "图片理解不可用",
    detail: "当前对话模型不支持参考图。",
    recoverable: false,
  },
  agent_reasoning_model_unavailable: {
    title: "推理能力不可用",
    detail: "当前对话模型不支持所选推理强度。",
    recoverable: false,
  },
  agent_context_window_exceeded: {
    title: "上下文容量不足",
    detail: "当前模型无法容纳该会话的完整文本和图片上下文。",
    recoverable: false,
  },
  agent_tool_result_unknown: {
    title: "提交结果待确认",
    detail: "不会自动重复有成本的图片请求。",
    recoverable: true,
  },
  agent_limit_reached: {
    title: "运行已达上限",
    detail: "已保留当前文本和图片任务。",
    recoverable: false,
  },
  agent_tool_limit_reached: {
    title: "生图调用达到上限",
    detail: "已保留当前文本和已接受的图片任务。",
    recoverable: false,
  },
  agent_image_limit_reached: {
    title: "生图数量达到上限",
    detail: "已保留当前文本和已接受的图片任务。",
    recoverable: false,
  },
  agent_reference_limit_reached: {
    title: "参考图超过上限",
    detail: "Agent 每条消息最多使用 16 张参考图。",
    recoverable: false,
  },
  agent_session_reference_limit_reached: {
    title: "会话图片达到上限",
    detail: "当前 Agent 会话已保留 64 张图片，请先移除不再使用的图片。",
    recoverable: false,
  },
  agent_run_not_continuable: {
    title: "无法安全继续",
    detail: "该运行包含图片或工具状态，请编辑后重新提交。",
    recoverable: false,
  },
  INSUFFICIENT_BALANCE: {
    title: "余额不足",
    detail: "充值后可继续运行 Agent。",
    recoverable: false,
    href: "/me/wallet",
    actionLabel: "查看钱包",
  },
  NO_ACTIVE_API_KEY: {
    title: "API 密钥不可用",
    detail: "添加有效密钥后可继续运行 Agent。",
    recoverable: false,
    href: "/settings/api-key",
    actionLabel: "管理密钥",
  },
  agent_run_active: {
    title: "Agent 运行中",
    detail: "等待当前运行结束，或先停止。",
    recoverable: false,
  },
  network_error: {
    title: "网络异常",
    detail: "消息可能已提交，正在核对快照。",
    recoverable: true,
  },
};

export function agentErrorPresentation(
  error: unknown,
): AgentErrorPresentation {
  const code =
    error instanceof ApiError
      ? error.code
      : error && typeof error === "object" && "code" in error
        ? String((error as { code?: unknown }).code ?? "")
        : "";
  return (
    ERROR_PRESENTATIONS[code] ?? {
      title: "Agent 运行失败",
      detail: "当前结果已保留，可以重试或新建会话。",
      recoverable: true,
    }
  );
}

export function agentRunErrorPresentation(
  errorCode: string | null | undefined,
): AgentErrorPresentation {
  return agentErrorPresentation({ code: errorCode ?? "" });
}
