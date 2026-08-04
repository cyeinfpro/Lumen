import type { ReactNode } from "react";
import { EyeOff } from "lucide-react";

import {
  Button,
  MetricCard as PrimitiveMetricCard,
  Switch,
} from "@/components/ui/primitives";

export function MetricCard({
  label,
  value,
  icon,
  sub,
}: {
  label: string;
  value: string;
  icon: ReactNode;
  sub?: ReactNode;
}) {
  return (
    <PrimitiveMetricCard label={label} value={value} icon={icon} sub={sub} />
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
            <p className="type-body-sm text-[var(--fg-0)]">兑换码 secret</p>
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
        <div className="mt-3 flex items-center justify-between gap-3 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-2)] px-3 py-2">
          <span className="type-caption text-[var(--fg-2)]">
            我确认轮换 secret 会作废所有未兑换码
          </span>
          <Switch
            checked={confirmed}
            onCheckedChange={onConfirmedChange}
            aria-label="确认轮换兑换码 secret"
          />
        </div>
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
      <div className="flex min-h-10 w-full items-center justify-between rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3">
        <span className="type-body-sm text-[var(--fg-1)]">
          {checked ? "开启" : "关闭"}
        </span>
        <Switch
          checked={checked}
          onCheckedChange={onChange}
          aria-label={label}
        />
      </div>
    </div>
  );
}
