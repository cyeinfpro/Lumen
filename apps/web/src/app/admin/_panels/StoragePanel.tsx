"use client";

// Lumen 管理面板：存储后端（local / SMB）。
//
// 后端契约见 apps/api/app/routes/admin_storage.py + apps/web/src/lib/api/storage.ts。
// 关键 UX 点：PUT 后 host 会 docker stop lumen-api（约 10–30 秒），所以提交后必须：
//   1. 立即显示"应用中…"loading（关闭按钮 disabled）
//   2. 6 秒后开始 polling GET /admin/storage 每 3 秒
//   3. 通过 last_apply.call_id === 我们刚拿到的 call_id 且 status !== "pending" 判定完成
//   4. 90 秒还没拿到 → 超时 toast 提示用户刷新
//
// 这里所有 setState 都在事件回调或 effect 内（不在 render 阶段读 ref / setState），
// 符合 React 19 hooks 规则。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  AlertTriangle,
  CheckCircle2,
  HardDrive,
  Loader2,
  Network,
  RotateCcw,
  Save,
  Server,
  ShieldAlert,
  Wifi,
  XCircle,
} from "lucide-react";

import {
  qk,
  useAdminStorageQuery,
  usePutAdminStorageMutation,
  useTestAdminStorageMutation,
} from "@/lib/queries";
import type {
  StorageConfigOut,
  StorageConfigUpdateIn,
  StorageTestIn,
} from "@/lib/api/storage";
import { getAdminStorage } from "@/lib/api/storage";
import { ApiError } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import {
  Button,
  ConfirmDialog,
  Input,
  toast,
} from "@/components/ui/primitives";

// ————————————————————————————————————————————
// 常量
// ————————————————————————————————————————————

const DEFAULT_LOCAL_ROOT = "/var/lib/lumen-data";
// PUT 之后 lumen-api 容器会被 stop → restart；polling 早开会全部 throw（无意义请求）。
const POLL_DELAY_MS = 6_000;
const POLL_INTERVAL_MS = 3_000;
const POLL_TIMEOUT_MS = 90_000;

type Backend = "local" | "smb";

interface FormState {
  backend: Backend;
  localRoot: string;
  host: string;
  share: string;
  subpath: string;
  username: string;
  password: string; // "" 表示保留旧密码
}

function deriveInitialForm(cfg: StorageConfigOut | undefined): FormState {
  // backend 为空字符串（host 还没初始化）时默认本地
  const backendRaw = cfg?.backend;
  const backend: Backend = backendRaw === "smb" ? "smb" : "local";
  return {
    backend,
    localRoot:
      cfg?.local?.root || cfg?.status?.target || DEFAULT_LOCAL_ROOT,
    host: cfg?.smb?.host ?? "",
    share: cfg?.smb?.share ?? "",
    subpath: cfg?.smb?.subpath ?? "",
    username: cfg?.smb?.username ?? "",
    password: "",
  };
}

// ————————————————————————————————————————————
// 入口
// ————————————————————————————————————————————

export function StoragePanel() {
  const q = useAdminStorageQuery();
  const cfg = q.data;

  const [form, setForm] = useState<FormState>(() => deriveInitialForm(undefined));
  const lastSyncedSigRef = useRef<string>("");

  // 后端数据回填（只在变化时刷新本地表单，避免覆盖用户正在输入的内容）
  useEffect(() => {
    if (!cfg) return;
    const sig = JSON.stringify({
      backend: cfg.backend,
      local: cfg.local,
      smb: { ...cfg.smb },
    });
    if (sig === lastSyncedSigRef.current) return;
    lastSyncedSigRef.current = sig;
    setForm(deriveInitialForm(cfg));
  }, [cfg]);

  if (q.isLoading && !cfg) {
    return (
      <section className="space-y-5">
        <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 p-6 backdrop-blur-sm">
          <div className="flex items-center gap-3 text-sm text-[var(--fg-1)]">
            <Loader2 className="h-4 w-4 animate-spin" /> 加载存储配置中
          </div>
        </div>
      </section>
    );
  }

  if (q.isError && !cfg) {
    return (
      <section className="space-y-5">
        <div className="rounded-2xl border border-red-500/30 bg-red-500/5 p-6 text-sm text-red-200">
          <div className="flex items-start gap-3">
            <ShieldAlert className="h-5 w-5 shrink-0 text-red-300" />
            <div className="min-w-0">
              <p className="font-medium">读取存储配置失败</p>
              <p className="mt-1 text-xs text-red-300/80">
                {q.error?.message ?? "未知错误"}
              </p>
              <Button
                size="sm"
                variant="secondary"
                className="mt-3"
                onClick={() => void q.refetch()}
                leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
              >
                重试
              </Button>
            </div>
          </div>
        </div>
      </section>
    );
  }

  if (!cfg) return null;

  return <StorageInner cfg={cfg} form={form} setForm={setForm} />;
}

// ————————————————————————————————————————————
// 主体
// ————————————————————————————————————————————

interface StorageInnerProps {
  cfg: StorageConfigOut;
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
}

function StorageInner({ cfg, form, setForm }: StorageInnerProps) {
  const qc = useQueryClient();
  const testMut = useTestAdminStorageMutation();
  const putMut = usePutAdminStorageMutation();

  const [confirmOpen, setConfirmOpen] = useState(false);
  // applying：已 PUT 成功收到 call_id，正在 polling 等终态
  const [applying, setApplying] = useState<{
    callId: string;
    startedAt: number;
  } | null>(null);
  const pollingTimerRef = useRef<number | null>(null);
  const timeoutTimerRef = useRef<number | null>(null);
  const startDelayTimerRef = useRef<number | null>(null);
  const applyToastIdRef = useRef<string | null>(null);

  // —— 表单校验 / dirty —— //
  const canTestSmb = useMemo(() => {
    if (form.backend !== "smb") return false;
    if (testMut.isPending) return false;
    return Boolean(form.host.trim() && form.share.trim() && form.username.trim());
  }, [form.backend, form.host, form.share, form.username, testMut.isPending]);

  const formError = useMemo(() => {
    if (form.backend === "local") {
      const root = form.localRoot.trim();
      if (!root) return "请填写本机目录";
      if (!root.startsWith("/")) return "本机目录必须是绝对路径，以 / 开头";
      return null;
    }
    if (!form.host.trim()) return "请填写 SMB host";
    if (!form.share.trim()) return "请填写 share 名";
    if (!form.username.trim()) return "请填写用户名";
    if (!cfg.smb.has_password && !form.password) return "请输入密码";
    return null;
  }, [
    cfg.smb.has_password,
    form.backend,
    form.host,
    form.localRoot,
    form.password,
    form.share,
    form.username,
  ]);

  const isApplying = applying != null;
  const submitDisabled = isApplying || putMut.isPending || formError != null;

  // —— polling 收尾 —— //
  const stopPolling = useCallback(() => {
    if (pollingTimerRef.current != null) {
      window.clearTimeout(pollingTimerRef.current);
      pollingTimerRef.current = null;
    }
    if (timeoutTimerRef.current != null) {
      window.clearTimeout(timeoutTimerRef.current);
      timeoutTimerRef.current = null;
    }
    if (startDelayTimerRef.current != null) {
      window.clearTimeout(startDelayTimerRef.current);
      startDelayTimerRef.current = null;
    }
  }, []);

  // 卸载兜底：用户切走 tab 时清理 timer
  useEffect(() => {
    return () => {
      stopPolling();
      if (applyToastIdRef.current) {
        toast.dismiss(applyToastIdRef.current);
        applyToastIdRef.current = null;
      }
    };
  }, [stopPolling]);

  // —— polling 主循环 —— //
  const finishApply = useCallback(
    (kind: "ok" | "fail" | "timeout", message: string) => {
      stopPolling();
      setApplying(null);
      if (applyToastIdRef.current) {
        toast.dismiss(applyToastIdRef.current);
        applyToastIdRef.current = null;
      }
      if (kind === "ok") {
        toast.success("存储后端切换完成", { description: message });
      } else if (kind === "fail") {
        toast.error("切换失败", { description: message, durationMs: 8000 });
      } else {
        toast.warning("应用可能仍在进行", {
          description: message,
          durationMs: 8000,
        });
      }
      // 不论结果，都触发一次 refetch 让卡片显示最新状态
      qc.invalidateQueries({ queryKey: qk.adminStorage() });
    },
    [qc, stopPolling],
  );

  // 用 ref 持有 pollOnce，避免 useCallback 自我引用（react-hooks immutability lint）。
  // ref 永远指向最新一份函数；递归调用走 ref 即可。
  // 写入放在 useEffect（render 阶段不直接 mutate ref，遵循 React 19 lint）。
  const pollOnceRef = useRef<(callId: string, deadline: number) => Promise<void>>(
    async () => {},
  );
  useEffect(() => {
    pollOnceRef.current = async (callId: string, deadline: number) => {
      try {
        const fresh = await getAdminStorage();
        // 同步进 query cache，让卡片自动刷新
        qc.setQueryData(qk.adminStorage(), fresh);
        const apply = fresh.last_apply;
        if (apply && apply.call_id === callId && apply.status !== "pending") {
          if (apply.status === "ok") {
            finishApply("ok", apply.message || "切换成功");
          } else {
            finishApply("fail", apply.message || "切换失败");
          }
          return;
        }
      } catch {
        // lumen-api 还在重启 / 网络抖动 → 静默重试
      }
      if (Date.now() >= deadline) {
        finishApply("timeout", "操作可能仍在进行，请刷新页面查看最终状态。");
        return;
      }
      pollingTimerRef.current = window.setTimeout(() => {
        void pollOnceRef.current(callId, deadline);
      }, POLL_INTERVAL_MS);
    };
  }, [finishApply, qc]);

  const beginPolling = useCallback(
    (callId: string) => {
      const deadline = Date.now() + POLL_TIMEOUT_MS;
      // 6s 之后再发首个请求（API 重启窗口期间 fetch 必失败）
      startDelayTimerRef.current = window.setTimeout(() => {
        void pollOnceRef.current(callId, deadline);
      }, POLL_DELAY_MS);
      // 兜底定时器：到期未结束则 timeout
      timeoutTimerRef.current = window.setTimeout(() => {
        finishApply("timeout", "等待超时，请刷新页面查看最终状态。");
      }, POLL_TIMEOUT_MS);
    },
    [finishApply],
  );

  // —— 提交 —— //
  const submit = useCallback(() => {
    if (formError) return;
    const payload: StorageConfigUpdateIn =
      form.backend === "local"
        ? {
            backend: "local",
            local: { root: form.localRoot.trim() },
            smb: null,
          }
        : {
            backend: "smb",
            local: null,
            smb: {
              host: form.host.trim(),
              share: form.share.trim(),
              subpath: form.subpath.trim(),
              username: form.username.trim(),
              password: form.password,
            },
          };

    putMut.mutate(payload, {
      onSuccess: (res) => {
        setConfirmOpen(false);
        // 立即记录 applying；toast 用 0 duration 自管理，结束时手动 dismiss
        const tid = toast.info("正在切换存储后端", {
          description: "API 即将重启，约 10–30 秒。请勿关闭页面。",
          durationMs: 0,
        });
        applyToastIdRef.current = tid;
        setApplying({ callId: res.call_id, startedAt: Date.now() });
        // 把 PUT 返回的 config 先写进 cache（last_apply.status=pending）
        qc.setQueryData(qk.adminStorage(), res.config);
        beginPolling(res.call_id);
      },
      onError: (err) => {
        setConfirmOpen(false);
        const msg =
          err instanceof ApiError ? err.message : err.message || "切换失败";
        toast.error("提交切换请求失败", { description: msg, durationMs: 6000 });
      },
    });
  }, [beginPolling, form, formError, putMut, qc]);

  // —— 测试 SMB —— //
  const onTest = useCallback(() => {
    if (!canTestSmb) return;
    const body: StorageTestIn = {
      host: form.host.trim(),
      share: form.share.trim(),
      subpath: form.subpath.trim(),
      username: form.username.trim(),
      password: form.password,
    };
    testMut.mutate(body, {
      onSuccess: (res) => {
        if (res.status === "ok") {
          toast.success("SMB 连接成功", {
            description: res.message || undefined,
          });
        } else if (res.status === "fail") {
          toast.error("SMB 连接失败", {
            description: res.message || undefined,
            durationMs: 8000,
          });
        } else {
          toast.info("测试已提交", {
            description: res.message || "正在执行，请稍后查看结果",
          });
        }
        // 测试完成后刷新 last_test 显示
        void qc.invalidateQueries({ queryKey: qk.adminStorage() });
      },
      onError: (err) => {
        const msg =
          err instanceof ApiError ? err.message : err.message || "测试失败";
        toast.error("测试请求失败", { description: msg, durationMs: 6000 });
      },
    });
  }, [canTestSmb, form, qc, testMut]);

  return (
    <section className="space-y-5 pb-12">
      <StatusCard cfg={cfg} applying={isApplying} />

      <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 p-4 backdrop-blur-sm md:p-5">
        <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div className="flex min-w-0 items-start gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-[var(--color-lumen-amber)]/25 bg-[var(--color-lumen-amber)]/12">
              <HardDrive className="h-4 w-4 text-[var(--color-lumen-amber)]" />
            </div>
            <div className="min-w-0">
              <h3 className="text-sm font-medium text-neutral-100">
                存储后端
              </h3>
              <p className="mt-1 text-xs leading-5 text-neutral-500">
                Lumen 用户上传 / 生成的图片落到这里。切换后会重启 API 容器，
                <span className="text-neutral-300">不会自动迁移历史数据</span>。
              </p>
            </div>
          </div>
        </div>

        {/* —— backend 选择 —— */}
        <div className="mt-5">
          <BackendSwitch
            value={form.backend}
            disabled={isApplying}
            onChange={(next) => setForm((s) => ({ ...s, backend: next }))}
          />
        </div>

        {/* —— 表单区 —— */}
        <div className="mt-5 space-y-4">
          {form.backend === "local" ? (
            <LocalForm
              root={form.localRoot}
              disabled={isApplying}
              onChange={(v) => setForm((s) => ({ ...s, localRoot: v }))}
            />
          ) : (
            <SmbForm
              form={form}
              hasPassword={cfg.smb.has_password}
              disabled={isApplying}
              onChange={(patch) => setForm((s) => ({ ...s, ...patch }))}
            />
          )}
        </div>

        {/* —— 操作区 —— */}
        <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="text-xs text-neutral-500">
            {formError ? (
              <span className="inline-flex items-center gap-1.5 text-red-300">
                <AlertTriangle className="h-3.5 w-3.5" /> {formError}
              </span>
            ) : isApplying ? (
              <span className="inline-flex items-center gap-1.5 text-sky-300">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                应用中，API 重启中…
              </span>
            ) : (
              "确认无误后点击右侧按钮提交"
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            {form.backend === "smb" && (
              <Button
                variant="secondary"
                size="sm"
                onClick={onTest}
                disabled={!canTestSmb}
                loading={testMut.isPending}
                leftIcon={!testMut.isPending ? <Wifi className="h-3.5 w-3.5" /> : undefined}
              >
                测试 SMB 连接
              </Button>
            )}
            <Button
              variant="primary"
              size="sm"
              onClick={() => setConfirmOpen(true)}
              disabled={submitDisabled}
              loading={isApplying}
              leftIcon={!isApplying ? <Save className="h-3.5 w-3.5" /> : undefined}
            >
              {isApplying ? "应用中" : "应用并切换"}
            </Button>
          </div>
        </div>
      </div>

      <RecoveryHints />

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={(open) => {
          if (!putMut.isPending && !isApplying) setConfirmOpen(open);
        }}
        title="确认切换存储后端？"
        description={
          <div className="space-y-2">
            <p>
              提交后会
              <span className="text-[var(--fg-0)] font-medium">
                重启 API 容器
              </span>
              ，约 10–30 秒不可访问。
            </p>
            <p className="text-[var(--danger)]/90">
              切换不会自动迁移已有数据。请确认目标位置上的内容是你需要的。
            </p>
          </div>
        }
        confirmText="确认应用"
        cancelText="再看看"
        tone="danger"
        confirming={putMut.isPending}
        onConfirm={submit}
      />
    </section>
  );
}

// ————————————————————————————————————————————
// 状态卡
// ————————————————————————————————————————————

function StatusCard({
  cfg,
  applying,
}: {
  cfg: StorageConfigOut;
  applying: boolean;
}) {
  const status = cfg.status;
  const lastApply = cfg.last_apply;
  const lastTest = cfg.last_test;

  // 模式判定：applying > status.disabled > status.mounted
  const tone = applying
    ? "pending"
    : status?.disabled
      ? "warning"
      : status?.mounted
        ? "ok"
        : "warning";

  const toneClasses: Record<typeof tone, string> = {
    ok: "border-emerald-500/30 bg-emerald-500/8 text-emerald-200",
    warning: "border-amber-500/30 bg-amber-500/8 text-amber-200",
    pending: "border-sky-500/30 bg-sky-500/8 text-sky-200",
  };

  const headLine = applying
    ? "正在应用…"
    : status == null
      ? "host 还未上报状态"
      : status.disabled
        ? "已强制回退到本地默认路径"
        : status.mounted
          ? "存储已就绪"
          : "存储未挂载";

  const modeLabel =
    cfg.backend === "smb"
      ? "SMB"
      : cfg.backend === "local"
        ? "本机目录"
        : "未配置";

  return (
    <div
      className={cn(
        "rounded-2xl border bg-[var(--bg-1)]/60 p-4 backdrop-blur-sm md:p-5",
        "border-white/10",
      )}
    >
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <div
            className={cn(
              "flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border",
              tone === "ok"
                ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                : tone === "warning"
                  ? "border-amber-500/30 bg-amber-500/10 text-amber-300"
                  : "border-sky-500/30 bg-sky-500/10 text-sky-300",
            )}
          >
            {tone === "ok" ? (
              <CheckCircle2 className="h-4 w-4" />
            ) : tone === "warning" ? (
              <AlertTriangle className="h-4 w-4" />
            ) : (
              <Loader2 className="h-4 w-4 animate-spin" />
            )}
          </div>
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium text-neutral-100">
                {headLine}
              </span>
              <span
                className={cn(
                  "rounded-md border px-2 py-0.5 text-[11px]",
                  toneClasses[tone],
                )}
              >
                {modeLabel}
              </span>
              {status?.disabled && (
                <span className="rounded-md border border-amber-500/30 bg-amber-500/8 px-2 py-0.5 text-[11px] text-amber-200">
                  禁用 flag 已生效
                </span>
              )}
            </div>
            {status && (
              <div className="text-xs leading-5 text-neutral-400">
                target{" "}
                <code className="rounded bg-white/5 px-1.5 py-0.5 font-mono text-[11px] text-neutral-200">
                  {status.target || "—"}
                </code>{" "}
                · fstype{" "}
                <span className="font-mono text-neutral-200">
                  {status.fstype || "—"}
                </span>
                {status.source && (
                  <>
                    {" "}
                    · source{" "}
                    <span className="font-mono break-all text-neutral-200">
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
            <Badge tone="muted">
              更新 {formatTs(status.updated_at)}
            </Badge>
          )}
          {lastApply && (
            <Badge
              tone={
                lastApply.status === "ok"
                  ? "ok"
                  : lastApply.status === "fail"
                    ? "fail"
                    : "info"
              }
            >
              上次应用 {applyStatusLabel(lastApply.status)}
            </Badge>
          )}
        </div>
      </div>

      {(lastApply?.message || lastTest) && (
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          {lastApply?.message && (
            <SubLine
              icon={
                lastApply.status === "ok" ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-300" />
                ) : lastApply.status === "fail" ? (
                  <XCircle className="h-3.5 w-3.5 text-red-300" />
                ) : (
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-sky-300" />
                )
              }
              label="上次应用"
              detail={lastApply.message}
              ts={lastApply.finished_at || lastApply.started_at}
            />
          )}
          {lastTest && (
            <SubLine
              icon={
                lastTest.status === "ok" ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-300" />
                ) : (
                  <XCircle className="h-3.5 w-3.5 text-red-300" />
                )
              }
              label="上次测试"
              detail={lastTest.message}
              ts={lastTest.tested_at}
            />
          )}
        </div>
      )}
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
    <div className="flex items-start gap-2 rounded-xl border border-white/8 bg-white/[0.02] px-3 py-2 text-xs">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="text-neutral-300">{label}</span>
          <span className="font-mono text-[10px] tabular-nums text-neutral-500">
            {formatTs(ts)}
          </span>
        </div>
        <p className="mt-0.5 break-words text-neutral-400">{detail}</p>
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
      ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
      : tone === "fail"
        ? "border-red-500/30 bg-red-500/10 text-red-200"
        : tone === "info"
          ? "border-sky-500/30 bg-sky-500/10 text-sky-200"
          : "border-white/10 bg-white/[0.04] text-neutral-400";
  return (
    <span className={cn("inline-flex items-center rounded-md border px-2 py-0.5", cls)}>
      {children}
    </span>
  );
}

// ————————————————————————————————————————————
// 表单
// ————————————————————————————————————————————

function BackendSwitch({
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
          <button
            key={o.key}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={disabled}
            onClick={() => onChange(o.key)}
            className={cn(
              "flex items-start gap-3 rounded-xl border px-3 py-2.5 text-left transition-colors",
              "disabled:cursor-not-allowed disabled:opacity-60",
              active
                ? "border-[var(--color-lumen-amber)]/45 bg-[var(--color-lumen-amber)]/8"
                : "border-white/10 bg-white/[0.02] hover:bg-white/[0.04]",
            )}
          >
            <span
              className={cn(
                "mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border",
                active
                  ? "border-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)]/15"
                  : "border-white/15 bg-white/[0.04]",
              )}
            >
              {active && (
                <motion.span
                  layoutId="storage-radio-dot"
                  className="h-2 w-2 rounded-full bg-[var(--color-lumen-amber)]"
                  transition={{ type: "spring", stiffness: 380, damping: 26 }}
                />
              )}
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-1.5 text-sm font-medium text-neutral-100">
                {o.icon}
                {o.label}
              </span>
              <span className="mt-0.5 block text-[11px] leading-relaxed text-neutral-500">
                {o.hint}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function LocalForm({
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

function SmbForm({
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

function RecoveryHints() {
  return (
    <div className="rounded-2xl border border-white/8 bg-white/[0.02] p-4 text-xs leading-relaxed text-neutral-400">
      <div className="flex items-start gap-2">
        <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-300" />
        <div className="space-y-1.5">
          <p>
            如果 SMB 挂不上，SSH 到 host 上创建{" "}
            <code className="rounded bg-white/8 px-1 py-0.5 font-mono text-[11px] text-neutral-200">
              /var/lib/lumen-storage/disabled
            </code>{" "}
            文件可强制回退到本地默认路径并恢复服务。
          </p>
          <p className="text-neutral-500">
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

export default StoragePanel;
