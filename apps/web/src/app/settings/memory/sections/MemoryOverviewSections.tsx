import type { ReactNode } from "react";
import Link from "next/link";
import {
  AlertTriangle,
  Brain,
  Pause,
  ShieldOff,
} from "lucide-react";

import { Button, Switch } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";

import type { MemorySettingsData } from "../types";

export function MemoryCapabilityBanner({
  available,
}: {
  available: boolean;
}) {
  if (available) return null;
  return (
    <section className="flex flex-col gap-3 rounded-[var(--radius-card)] border border-warning-border bg-warning-soft p-4 type-body-sm sm:flex-row sm:items-center sm:justify-between">
      <div className="flex gap-3">
        <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-warning" />
        <div>
          <div className="type-body-sm font-medium text-warning">记忆未启用</div>
          <p className="mt-1 type-caption leading-5 text-warning/80">
            需先在管理员后台为某个 provider 勾选
            “embedding”；写入、检索、抽取均依赖向量。
          </p>
        </div>
      </div>
      <Link
        href="/admin"
        className="inline-flex min-h-11 flex-shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-warning-border bg-warning-soft px-3 type-caption font-medium text-warning transition-colors hover:bg-warning/20"
      >
        去管理员后台 →
      </Link>
    </section>
  );
}

export function MemorySettingsToggles({
  settings,
  embeddingAvailable,
  pending,
  onEnableChange,
  onPausedChange,
  onConfirmationChange,
}: {
  settings: MemorySettingsData | undefined;
  embeddingAvailable: boolean;
  pending: boolean;
  onEnableChange: (checked: boolean) => void;
  onPausedChange: (checked: boolean) => void;
  onConfirmationChange: (checked: boolean) => void;
}) {
  const memoryDisabled = Boolean(settings?.disabled);
  const dependentDisabled =
    pending || !embeddingAvailable || memoryDisabled;
  return (
    <section className="grid gap-3 md:grid-cols-3">
      <SettingToggle
        icon={<Brain className="h-4 w-4" />}
        title="启用记忆"
        description="开启后 Lumen 会从对话中学习稳定偏好，并在新会话里复用。"
        checked={!memoryDisabled}
        disabled={pending}
        onChange={onEnableChange}
      />
      <SettingToggle
        icon={<Pause className="h-4 w-4" />}
        title="暂停学习"
        description="不写入新记忆,已有记忆仍会参与回答。"
        checked={Boolean(settings?.paused)}
        disabled={dependentDisabled}
        onChange={onPausedChange}
      />
      <SettingToggle
        icon={<ShieldOff className="h-4 w-4" />}
        title="主动确认偏好"
        description="强偏好命中时,偶尔让模型先确认。"
        checked={Boolean(settings?.confirmation_enabled)}
        disabled={dependentDisabled}
        onChange={onConfirmationChange}
      />
    </section>
  );
}

export function MemoryFirstRunCard({
  visible,
  onPause,
  onConfirm,
}: {
  visible: boolean;
  onPause: () => void;
  onConfirm: () => void;
}) {
  if (!visible) return null;
  return (
    <section className="rounded-[var(--radius-card)] border border-accent-border bg-accent-soft p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="type-body-sm font-medium text-[var(--fg-0)]">
            Lumen 会从对话里学到稳定偏好
          </div>
          <p className="mt-1 type-body-sm text-[var(--fg-1)]">
            也可以在这里手动添加，比如“偏好简洁回答”或“不要使用感叹号”。
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={onPause}>
            先暂停
          </Button>
          <Button variant="primary" size="sm" onClick={onConfirm}>
            {copy.action.confirm}
          </Button>
        </div>
      </div>
    </section>
  );
}

function SettingToggle({
  icon,
  title,
  description,
  checked,
  disabled,
  onChange,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div
      className={[
        "flex min-h-[112px] items-start gap-3 rounded-[var(--radius-card)] border p-4 text-left transition-colors",
        checked
          ? "border-accent-border bg-accent-soft"
          : "border-[var(--border-subtle)] bg-[var(--bg-1)]/60 hover:bg-[var(--bg-3)]",
      ].join(" ")}
    >
      <span className="mt-0.5 text-accent">{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block type-body-sm font-medium text-[var(--fg-0)]">
          {title}
        </span>
        <span className="mt-1 block type-caption leading-5 text-[var(--fg-2)]">
          {description}
        </span>
      </span>
      <Switch
        checked={checked}
        disabled={disabled}
        onCheckedChange={onChange}
        aria-label={title}
        className="mt-1"
      />
    </div>
  );
}
