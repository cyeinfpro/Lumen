"use client";

import type React from "react";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  CheckCircle2,
  HardDrive,
  Loader2,
  Network,
  Server,
  ShieldAlert,
  XCircle,
} from "lucide-react";

import type { StorageConfigOut } from "@/lib/api/storage";
import { Input } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import {
  DEFAULT_LOCAL_ROOT,
  type StorageBackend as Backend,
  type StorageFormState as FormState,
} from "./StoragePanelTypes";

type StorageTone = "ok" | "warning" | "pending";

function storageTone(
  applying: boolean,
  status: StorageConfigOut["status"],
): StorageTone {
  if (applying) return "pending";
  if (status?.disabled) return "warning";
  return status?.mounted ? "ok" : "warning";
}

function storageToneClasses(tone: StorageTone): string {
  switch (tone) {
    case "ok":
      return "border-success-border bg-success-soft text-success";
    case "warning":
      return "border-warning-border bg-warning-soft text-warning";
    default:
      return "border-info-border bg-info-soft text-info";
  }
}

function storageHeadLine(
  applying: boolean,
  status: StorageConfigOut["status"],
): string {
  if (applying) return "正在应用…";
  if (!status) return "host 还未上报状态";
  if (status.disabled) return "已强制回退到本地默认路径";
  return status.mounted ? "存储已就绪" : "存储未挂载";
}

function storageModeLabel(backend: StorageConfigOut["backend"]): string {
  if (backend === "smb") return "SMB";
  if (backend === "local") return "本机目录";
  return "未配置";
}

function StorageToneIcon({ tone }: { tone: StorageTone }) {
  if (tone === "ok") return <CheckCircle2 className="h-4 w-4" />;
  if (tone === "warning") return <AlertTriangle className="h-4 w-4" />;
  return <Loader2 className="h-4 w-4 animate-spin" />;
}

function StorageStatusHeader({
  cfg,
  applying,
  tone,
}: {
  cfg: StorageConfigOut;
  applying: boolean;
  tone: StorageTone;
}) {
  const status = cfg.status;
  return (
    <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
      <div className="flex min-w-0 items-start gap-3">
        <div
          className={cn(
            "flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-card)] border",
            storageToneClasses(tone),
          )}
        >
          <StorageToneIcon tone={tone} />
        </div>
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium text-[var(--fg-0)]">
              {storageHeadLine(applying, status)}
            </span>
            <span
              className={cn(
                "rounded-[var(--radius-control)] border px-2 py-0.5 text-[11px]",
                storageToneClasses(tone),
              )}
            >
              {storageModeLabel(cfg.backend)}
            </span>
            {status?.disabled && (
              <span className="rounded-[var(--radius-control)] border border-warning-border bg-warning-soft px-2 py-0.5 text-[11px] text-warning">
                禁用 flag 已生效
              </span>
            )}
          </div>
          {status && (
            <div className="text-xs leading-5 text-[var(--fg-1)]">
              target{" "}
              <code className="rounded bg-[var(--bg-2)] px-1.5 py-0.5 font-mono text-[11px] text-[var(--fg-0)]">
                {status.target || "—"}
              </code>{" "}
              · fstype{" "}
              <span className="font-mono text-[var(--fg-0)]">
                {status.fstype || "—"}
              </span>
              {status.source && (
                <>
                  {" "}
                  · source{" "}
                  <span className="font-mono break-all text-[var(--fg-0)]">
                    {status.source}
                  </span>
                </>
              )}
            </div>
          )}
        </div>
      </div>
      <div className="flex flex-wrap gap-2 text-[11px]">
        {status?.updated_at != null && (
          <Badge tone="muted">更新 {formatTs(status.updated_at)}</Badge>
        )}
        {cfg.last_apply && (
          <Badge tone={applyStatusTone(cfg.last_apply.status)}>
            上次应用 {applyStatusLabel(cfg.last_apply.status)}
          </Badge>
        )}
      </div>
    </div>
  );
}

function applyStatusTone(
  status: "ok" | "fail" | "pending",
): "ok" | "fail" | "info" {
  if (status === "ok") return "ok";
  if (status === "fail") return "fail";
  return "info";
}

function ApplyActivityIcon({ status }: { status: "ok" | "fail" | "pending" }) {
  if (status === "ok") {
    return <CheckCircle2 className="h-3.5 w-3.5 text-success" />;
  }
  if (status === "fail") {
    return <XCircle className="h-3.5 w-3.5 text-danger" />;
  }
  return <Loader2 className="h-3.5 w-3.5 animate-spin text-info" />;
}

function TestActivityIcon({ status }: { status: "ok" | "fail" }) {
  return status === "ok" ? (
    <CheckCircle2 className="h-3.5 w-3.5 text-success" />
  ) : (
    <XCircle className="h-3.5 w-3.5 text-danger" />
  );
}

function StorageActivity({
  lastApply,
  lastTest,
}: {
  lastApply: StorageConfigOut["last_apply"];
  lastTest: StorageConfigOut["last_test"];
}) {
  if (!lastApply?.message && !lastTest) return null;
  return (
    <div className="mt-4 grid gap-3 md:grid-cols-2">
      {lastApply?.message && (
        <SubLine
          icon={<ApplyActivityIcon status={lastApply.status} />}
          label="上次应用"
          detail={lastApply.message}
          ts={lastApply.finished_at || lastApply.started_at}
        />
      )}
      {lastTest && (
        <SubLine
          icon={<TestActivityIcon status={lastTest.status} />}
          label="上次测试"
          detail={lastTest.message}
          ts={lastTest.tested_at}
        />
      )}
    </div>
  );
}

export function StatusCard({
  cfg,
  applying,
}: {
  cfg: StorageConfigOut;
  applying: boolean;
}) {
  const tone = storageTone(applying, cfg.status);

  return (
    <div
      className={cn(
        "surface-card p-4 md:p-5",
        "border-[var(--border)]",
      )}
    >
      <StorageStatusHeader cfg={cfg} applying={applying} tone={tone} />
      <StorageActivity lastApply={cfg.last_apply} lastTest={cfg.last_test} />
    </div>
  );
}

function SubLine({
  icon,
  label,
  detail,
  ts,
}: {
  icon: React.ReactNode;
  label: string;
  detail: string;
  ts: number;
}) {
  return (
    <div className="flex items-start gap-2 rounded-[var(--radius-panel)] border border-[var(--border-subtle)] bg-[var(--bg-2)] px-3 py-2 text-xs">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[var(--fg-1)]">{label}</span>
          <span className="font-mono text-[10px] tabular-nums text-[var(--fg-2)]">
            {formatTs(ts)}
          </span>
        </div>
        <p className="mt-0.5 break-words text-[var(--fg-1)]">{detail}</p>
      </div>
    </div>
  );
}

function Badge({
  tone,
  children,
}: {
  tone: "ok" | "fail" | "info" | "muted";
  children: React.ReactNode;
}) {
  const cls =
    tone === "ok"
      ? "border-success-border bg-success-soft text-success"
      : tone === "fail"
        ? "border-danger-border bg-danger-soft text-danger"
        : tone === "info"
          ? "border-info-border bg-info-soft text-info"
          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]";
  return (
    <span className={cn("inline-flex items-center rounded-[var(--radius-control)] border px-2 py-0.5", cls)}>
      {children}
    </span>
  );
}

// ————————————————————————————————————————————
// 表单
// ————————————————————————————————————————————

export function BackendSwitch({
  value,
  onChange,
  disabled,
}: {
  value: Backend;
  onChange: (next: Backend) => void;
  disabled?: boolean;
}) {
  const opts: { key: Backend; label: string; icon: React.ReactNode; hint: string }[] = [
    {
      key: "local",
      label: "本机目录",
      icon: <HardDrive className="h-3.5 w-3.5" />,
      hint: "host 上的绝对路径，最简单可靠",
    },
    {
      key: "smb",
      label: "SMB 网络存储",
      icon: <Server className="h-3.5 w-3.5" />,
      hint: "挂载到远程 NAS / 文件服务器",
    },
  ];
  return (
    <div
      role="radiogroup"
      aria-label="存储后端"
      className="grid grid-cols-1 gap-2 sm:grid-cols-2"
    >
      {opts.map((o) => {
        const active = value === o.key;
        return (
          // 二选一卡片含图标+描述，多行内容不适合 Button primitive
          <button
            key={o.key}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={disabled}
            onClick={() => onChange(o.key)}
            className={cn(
              "flex items-start gap-3 rounded-[var(--radius-card)] border px-3 py-2.5 text-left transition-colors",
              "disabled:cursor-not-allowed disabled:opacity-60",
              active
                ? "border-[var(--accent)]/45 bg-[var(--accent)]/8"
                : "border-[var(--border)] bg-[var(--bg-2)] hover:bg-[var(--bg-3)]",
            )}
          >
            <span
              className={cn(
                "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border",
                active
                  ? "border-[var(--accent)] bg-[var(--accent)]/15"
                  : "border-[var(--border)] bg-[var(--bg-2)]",
              )}
            >
              {active && (
                <motion.span
                  layoutId="storage-radio-dot"
                  className="h-2 w-2 rounded-full bg-[var(--accent)]"
                  transition={{ type: "spring", stiffness: 380, damping: 26 }}
                />
              )}
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-1.5 text-sm font-medium text-[var(--fg-0)]">
                {o.icon}
                {o.label}
              </span>
              <span className="mt-0.5 block text-[11px] leading-relaxed text-[var(--fg-2)]">
                {o.hint}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

export function LocalForm({
  root,
  disabled,
  onChange,
}: {
  root: string;
  disabled?: boolean;
  onChange: (v: string) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-4">
      <Input
        label="本机目录（绝对路径）"
        value={root}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        placeholder={DEFAULT_LOCAL_ROOT}
        hint="host 上需可写；目录不存在时 host agent 会自动创建。"
        leftIcon={<HardDrive className="h-3.5 w-3.5" />}
      />
    </div>
  );
}

export function SmbForm({
  form,
  hasPassword,
  disabled,
  onChange,
}: {
  form: FormState;
  hasPassword: boolean;
  disabled?: boolean;
  onChange: (patch: Partial<FormState>) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      <Input
        label="Host"
        value={form.host}
        onChange={(e) => onChange({ host: e.target.value })}
        disabled={disabled}
        placeholder="nas.local 或 10.10.10.5"
        leftIcon={<Network className="h-3.5 w-3.5" />}
      />
      <Input
        label="端口（可选）"
        value={form.port}
        onChange={(e) => onChange({ port: e.target.value.replace(/[^0-9]/g, "") })}
        disabled={disabled}
        placeholder="445"
        inputMode="numeric"
        hint="留空走 SMB 默认 445。"
      />
      <Input
        label="Share"
        value={form.share}
        onChange={(e) => onChange({ share: e.target.value })}
        disabled={disabled}
        placeholder="lumen"
      />
      <Input
        label="子路径（可选）"
        value={form.subpath}
        onChange={(e) => onChange({ subpath: e.target.value })}
        disabled={disabled}
        placeholder="data/images"
        wrapperClassName="sm:col-span-2"
        hint="挂载点之下的相对子路径，留空表示用 share 根。"
      />
      <Input
        label="用户名"
        value={form.username}
        onChange={(e) => onChange({ username: e.target.value })}
        disabled={disabled}
        placeholder="lumen"
        autoComplete="off"
      />
      <Input
        label="密码"
        type="password"
        value={form.password}
        onChange={(e) => onChange({ password: e.target.value })}
        disabled={disabled}
        placeholder={hasPassword ? "留空表示保留已存密码" : "必填"}
        autoComplete="new-password"
        hint={
          hasPassword
            ? "已存在密码记录；如无需更换可留空。"
            : "首次配置请填写密码。"
        }
      />
    </div>
  );
}

// ————————————————————————————————————————————
// 底部提示
// ————————————————————————————————————————————

export function RecoveryHints() {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] p-4 type-caption leading-relaxed text-[var(--fg-2)]">
      <div className="flex items-start gap-2">
        <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
        <div className="space-y-1.5">
          <p>
            如果 SMB 挂不上，SSH 到 host 上创建{" "}
            <code className="rounded bg-[var(--bg-2)] px-1 py-0.5 font-mono text-[11px] text-[var(--fg-1)]">
              /var/lib/lumen-storage/disabled
            </code>{" "}
            文件可强制回退到本地默认路径并恢复服务。
          </p>
          <p className="text-[var(--fg-3)]">
            推荐 CIFS 参数已固化为{" "}
            <span className="font-mono">
              vers=3.1.1, soft, retrans=3, noperm, mfsymlinks, mapposix
            </span>
            ，无需手动配置。
          </p>
        </div>
      </div>
    </div>
  );
}

// ————————————————————————————————————————————
// utils
// ————————————————————————————————————————————

function applyStatusLabel(s: "ok" | "fail" | "pending"): string {
  if (s === "ok") return "成功";
  if (s === "fail") return "失败";
  return "进行中";
}

function formatTs(unixSeconds: number | null | undefined): string {
  if (!unixSeconds) return "—";
  try {
    // 后端 timestamp 是 unix seconds（float）
    const d = new Date(unixSeconds * 1000);
    if (Number.isNaN(d.getTime())) return "—";
    const pad = (n: number) => n.toString().padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {
    return "—";
  }
}
