import type { ReactNode } from "react";
import { EyeOff } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";

export function MetricCard({
  label,
  value,
  icon,
}: {
  label: string;
  value: string;
  icon: ReactNode;
}) {
  return (
    <Card variant="subtle" padding="md" className="space-y-2">
      <div className="flex items-center gap-2 text-[var(--fg-2)]">
        {icon}
        <span className="type-caption">{label}</span>
      </div>
      <p className="text-lg font-semibold tabular-nums text-[var(--fg-0)]">
        {value}
      </p>
    </Card>
  );
}

export function RedemptionSecretControl({
  configured,
  confirmed,
  loading,
  onConfirmedChange,
  onRotate,
}: {
  configured: boolean;
  confirmed: boolean;
  loading: boolean;
  onConfirmedChange: (confirmed: boolean) => void;
  onRotate: () => void;
}) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <EyeOff className="h-4 w-4 text-[var(--fg-2)]" />
          <div>
            <p className="text-sm text-[var(--fg-0)]">兑换码 secret</p>
            <p className="type-caption text-[var(--fg-2)]">
              {configured
                ? "已配置；轮换会撤销所有未兑换码"
                : "未配置；创建和兑换都会被拒绝"}
            </p>
          </div>
        </div>
        <div className="flex w-full justify-end sm:w-auto">
          <Button
            variant={configured ? "outline" : "primary"}
            size="md"
            disabled={configured && !confirmed}
            loading={loading}
            onClick={onRotate}
          >
            {configured ? "轮换" : "生成"}
          </Button>
        </div>
      </div>
      {configured && (
        <label className="mt-3 flex items-center gap-2 text-xs text-[var(--fg-2)]">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => onConfirmedChange(event.target.checked)}
          />
          我确认轮换 secret 会作废所有未兑换码
        </label>
      )}
    </div>
  );
}

export function SwitchField({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <div className="space-y-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={[
          "flex min-h-11 w-full items-center justify-between rounded-[var(--radius-control)] border px-3 text-sm",
          checked
            ? "border-[var(--accent)] bg-[var(--accent)]/15 text-[var(--fg-0)]"
            : "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]",
        ].join(" ")}
      >
        <span>{checked ? "开启" : "关闭"}</span>
        <span
          className={[
            "relative h-5 w-9 rounded-full transition-colors",
            checked ? "bg-[var(--accent)]" : "bg-[var(--bg-2)]",
          ].join(" ")}
        >
          <span
            className={[
              "absolute top-0.5 h-4 w-4 rounded-full bg-[var(--fg-0)]/90 transition-transform",
              checked ? "translate-x-4" : "translate-x-0.5",
            ].join(" ")}
          />
        </span>
      </button>
    </div>
  );
}
