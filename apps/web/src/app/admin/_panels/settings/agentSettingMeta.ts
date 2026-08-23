import { ShieldCheck, Timer } from "lucide-react";

export const AGENT_RUN_TIMEOUT_KEY = "agent.run_timeout_seconds";
export const AGENT_CAPABILITY_TTL_KEY = "agent.capability_ttl_seconds";

export const AGENT_TIMEOUT_SETTING_META = {
  [AGENT_RUN_TIMEOUT_KEY]: {
    group: "agent",
    title: "Agent 单次运行时限",
    summary: "限制一次 Agent 运行从开始到结束的最长时间。",
    detail: "总运行时限（秒）",
    kind: "integer",
    icon: Timer,
    unit: "秒",
    min: 10,
    max: 1500,
    defaultValue: "600",
    recommended: "默认：600 秒；长文本或多轮工具任务不建议低于 10 分钟。",
    keywords: ["agent", "run", "timeout", "运行", "超时"],
  },
  [AGENT_CAPABILITY_TTL_KEY]: {
    group: "agent",
    title: "Agent 工具凭证有效期",
    summary: "控制运行期间内部图片工具凭证的最长有效时间。",
    detail: "内部凭证时限（秒）",
    kind: "integer",
    icon: ShieldCheck,
    unit: "秒",
    min: 15,
    max: 3600,
    defaultValue: "900",
    recommended: "默认：900 秒；运行时会自动确保它覆盖总时限和工具调用时限。",
    keywords: ["agent", "capability", "ttl", "工具", "凭证"],
  },
} as const;
