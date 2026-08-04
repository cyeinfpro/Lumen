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
import {
  AlertTriangle,
  HardDrive,
  Loader2,
  RotateCcw,
  Save,
  ShieldAlert,
  Wifi,
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
import {
  Button,
  ConfirmDialog,
  toast,
} from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import {
  BackendSwitch,
  LocalForm,
  RecoveryHints,
  SmbForm,
  StatusCard,
} from "./StoragePanelPresentation";
import {
  DEFAULT_LOCAL_ROOT,
  type StorageBackend as Backend,
  type StorageFormState as FormState,
} from "./StoragePanelTypes";

// ————————————————————————————————————————————
// 常量
// ————————————————————————————————————————————

// PUT 之后 lumen-api 容器会被 stop → restart；polling 早开会全部 throw（无意义请求）。
const POLL_DELAY_MS = 6_000;
const POLL_INTERVAL_MS = 3_000;
const POLL_TIMEOUT_MS = 90_000;

// 表单字符串端口 → 后端 number；非数字 / 空 / 越界统一回 0（走默认 445）
function parsePortValue(raw: string): number {
  const v = parseInt(raw, 10);
  if (!Number.isFinite(v) || v < 1 || v > 65535) return 0;
  return v;
}

function backendFor(cfg: StorageConfigOut | undefined): Backend {
  return cfg?.backend === "smb" ? "smb" : "local";
}

function localRootFor(cfg: StorageConfigOut | undefined): string {
  return cfg?.local?.root || cfg?.status?.target || DEFAULT_LOCAL_ROOT;
}

function smbPortFor(cfg: StorageConfigOut | undefined): string {
  return cfg?.smb?.port ? String(cfg.smb.port) : "";
}

function deriveInitialForm(cfg: StorageConfigOut | undefined): FormState {
  return {
    backend: backendFor(cfg),
    localRoot: localRootFor(cfg),
    host: cfg?.smb?.host ?? "",
    port: smbPortFor(cfg),
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

  // 后端数据回填（只在变化时刷新本地表单，避免覆盖用户当前输入的内容）
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
        <div className="surface-card p-6">
          <div className="flex items-center gap-3 type-body-sm text-[var(--fg-1)]">
            <Loader2 className="h-4 w-4 animate-spin" /> 加载存储配置中
          </div>
        </div>
      </section>
    );
  }

  if (q.isError && !cfg) {
    return (
      <section className="space-y-5">
        <div className="rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-6 type-body-sm text-danger">
          <div className="flex items-start gap-3">
            <ShieldAlert className="h-5 w-5 shrink-0 text-danger" />
            <div className="min-w-0">
              <p className="font-medium">读取存储配置失败</p>
              <p className="mt-1 type-caption text-[var(--danger-fg)]">
                {q.error?.message ?? "未知错误"}
              </p>
              <Button
                size="sm"
                variant="secondary"
                className="mt-3"
                onClick={() => void q.refetch()}
                leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
              >
                {copy.action.retry}
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
  // applying：已 PUT 成功收到 call_id，轮询终态中
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
      if (!root) return "需填本机目录";
      if (!root.startsWith("/")) return "需绝对路径，/ 开头";
      return null;
    }
    if (!form.host.trim()) return "需填 SMB host";
    if (!form.share.trim()) return "需填 share 名";
    if (!form.username.trim()) return "需填用户名";
    if (!cfg.smb.has_password && !form.password) return "需填密码";
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
        finishApply("timeout", "操作可能仍在进行，刷新页面查看最终状态。");
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
        finishApply("timeout", "等待超时，刷新页面查看最终状态。");
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
              port: parsePortValue(form.port),
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
        const tid = toast.info("存储后端切换中", {
          description: "API 即将重启，约 10–30 秒。保持页面开启。",
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
      port: parsePortValue(form.port),
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
            description: res.message || "执行中，稍后查看结果",
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

      <div className="surface-card p-4 md:p-5">
        <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div className="flex min-w-0 items-start gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-card)] border border-[var(--accent)]/25 bg-[var(--accent)]/12">
              <HardDrive className="h-4 w-4 text-[var(--accent)]" />
            </div>
            <div className="min-w-0">
              <h3 className="type-card-title">存储后端</h3>
              <p className="mt-1 type-caption text-[var(--fg-2)] leading-5">
                Lumen 用户上传 / 生成的图片落到这里。切换后会重启 API 容器，
                <span className="text-[var(--fg-1)]">不会自动迁移历史数据</span>。
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
          <div aria-live="polite" className="type-caption text-[var(--fg-2)]">
            {formError ? (
              <span className="inline-flex items-center gap-1.5 text-danger">
                <AlertTriangle className="h-3.5 w-3.5" /> {formError}
              </span>
            ) : isApplying ? (
              <span className="inline-flex items-center gap-1.5 text-info">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                应用中，API 重启中
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
                测试连接
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
            <p className="text-[var(--danger-fg)]">
              切换不会自动迁移已有数据。确认目标位置上的内容是你需要的。
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
