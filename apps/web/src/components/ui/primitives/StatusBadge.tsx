"use client";

import { Badge, type BadgeProps, type BadgeTone } from "./Badge";

export interface StatusBadgeProps extends Omit<BadgeProps, "tone"> {
  status: string;
  tone?: BadgeTone;
  label?: React.ReactNode;
}

type StatusDefinition = {
  label: string;
  tone: BadgeTone;
};

const STATUS_DEFINITIONS: Record<string, StatusDefinition> = {
  active: { label: "已启用", tone: "success" },
  enabled: { label: "已启用", tone: "success" },
  on: { label: "开启", tone: "success" },
  valid: { label: "有效", tone: "success" },
  ready: { label: "就绪", tone: "success" },
  ok: { label: "正常", tone: "success" },
  success: { label: "成功", tone: "success" },
  succeeded: { label: "成功", tone: "success" },
  complete: { label: "已完成", tone: "success" },
  completed: { label: "已完成", tone: "success" },
  used: { label: "已使用", tone: "info" },
  pending: { label: "待处理", tone: "warning" },
  queued: { label: "排队中", tone: "warning" },
  processing: { label: "处理中", tone: "accent" },
  running: { label: "运行中", tone: "accent" },
  probing: { label: "探测中", tone: "accent" },
  warning: { label: "警告", tone: "warning" },
  warn: { label: "警告", tone: "warning" },
  expiring: { label: "即将过期", tone: "warning" },
  expired: { label: "已过期", tone: "warning" },
  revoked: { label: "已撤销", tone: "danger" },
  disabled: { label: "已停用", tone: "info" },
  inactive: { label: "未启用", tone: "info" },
  failed: { label: "失败", tone: "danger" },
  error: { label: "错误", tone: "danger" },
  canceled: { label: "已取消", tone: "info" },
  cancelled: { label: "已取消", tone: "info" },
  unknown: { label: "未知", tone: "info" },
  wallet: { label: "钱包", tone: "info" },
  byok: { label: "自带密钥", tone: "info" },
};

export function StatusBadge({
  status,
  tone,
  label,
  children,
  ref,
  ...props
}: StatusBadgeProps & { ref?: React.Ref<HTMLSpanElement> }) {
  const definition =
    STATUS_DEFINITIONS[status.trim().toLowerCase()] ?? {
      label: "未知",
      tone: "info" as const,
    };

  return (
    <Badge
      {...props}
      ref={ref}
      tone={tone ?? definition.tone}
    >
      {label ?? children ?? definition.label}
    </Badge>
  );
}
