"use client";

// Lumen V1.0 隐私 & 数据：
//  1. 导出我的数据（POST /me/export → zip）
//  2. 会话管理（list / revoke 单个；当前会话不可踢）
//  3. 删除账号（输入当前邮箱确认；DELETE /me → /login）
//
// 交互要点：
// - 分区卡片（导出 / 会话 / 危险区），每个区独立 header + 描述
// - 删除账号使用内嵌二次确认（非 window.confirm）
// - 会话列表踢下线同样走内嵌确认

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { format, formatDistanceToNow } from "date-fns";
import { zhCN } from "date-fns/locale";
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  Check,
  Download,
  LogOut,
  Monitor,
  Trash2,
} from "lucide-react";

import {
  useDeleteMyAccountMutation,
  useMySessionsQuery,
  useRevokeMySessionMutation,
} from "@/lib/queries";
import {
  ApiError,
  exportMyData,
  getMe,
  type AuthUser,
} from "@/lib/apiClient";
import type { SessionOut } from "@/lib/types";
import { useChatStore } from "@/store/useChatStore";
import { useUiStore } from "@/store/useUiStore";
import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";

const SESSION_SKELETON_KEYS = [
  "session-skeleton-current",
  "session-skeleton-secondary",
] as const;

export default function PrivacyPage() {
  const me = useQuery<AuthUser>({
    queryKey: ["me"],
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
  });

  return (
    <SettingsShell title="隐私 & 数据" subtitle="PRIVACY" maxWidth="max-w-4xl">
      <motion.div
        initial={false}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2, ease: "easeOut" }}
        className="space-y-6 pb-4 sm:space-y-8"
      >
        <header className="hidden items-start justify-between gap-4 flex-wrap md:flex">
          <div>
            <h1 className="type-page-title">
              隐私 & 数据
            </h1>
            <p className="type-body mt-1.5">
              查看数据、登录会话和账号删除选项。
            </p>
          </div>
          <Link
            href="/me"
            className="inline-flex min-h-9 items-center gap-1.5 px-2 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] whitespace-nowrap"
          >
            <ArrowLeft className="w-4 h-4" />
            返回我的
          </Link>
        </header>

        <ExportSection />
        <SessionsSection />
        <DangerSection email={me.data?.email ?? null} loading={me.isLoading} />
      </motion.div>
    </SettingsShell>
  );
}

function SectionHeader({
  title,
  description,
  tone = "neutral",
}: {
  title: string;
  description?: string;
  tone?: "neutral" | "danger";
}) {
  return (
    <div className="flex items-center gap-2">
      <h2
        className={
          "type-overline " +
          (tone === "danger" ? "text-danger" : "text-[var(--fg-1)]")
        }
      >
        {title}
      </h2>
      <div className="h-px flex-1 bg-[var(--border-subtle)]" />
      {description && (
        <p className="type-caption text-[var(--fg-2)]">{description}</p>
      )}
    </div>
  );
}

// ———————————————————————————— 导出数据 ————————————————————————————

function ExportSection() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [doneAt, setDoneAt] = useState<number | null>(null);

  const onExport = async () => {
    setError(null);
    setDoneAt(null);
    setBusy(true);
    // 保底：若 30s 内请求未返回（边缘情况如网络挂起），自动回置 busy，防止按钮永久禁用。
    const safety = setTimeout(() => {
      setBusy(false);
    }, 30_000);
    try {
      const blob = await exportMyData();
      const url = URL.createObjectURL(blob);
      const ts = format(new Date(), "yyyyMMdd-HHmmss");
      const a = document.createElement("a");
      a.href = url;
      a.download = `lumen-export-${ts}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
      setDoneAt(Date.now());
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message || `导出失败 (HTTP ${err.status})`);
      } else if (err instanceof Error) {
        setError(err.message || "导出失败");
      } else {
        setError("导出失败");
      }
    } finally {
      clearTimeout(safety);
      setBusy(false);
    }
  };

  return (
    <section className="space-y-3">
      <SectionHeader title="导出我的数据" />
      <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 backdrop-blur-sm p-5 flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-start gap-3 flex-1 min-w-[200px]">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]">
            <Download className="w-4 h-4" />
          </div>
          <div>
            <p className="type-body-sm text-[var(--fg-0)]">打包下载你的全部数据</p>
            <p className="type-caption text-[var(--fg-2)] mt-1">
              包含对话、消息和图像引用，输出为 zip（数秒至数十秒）。
            </p>
            <AnimatePresence>
              {doneAt && (
                <motion.p
                  initial={{ opacity: 0, y: -2 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  className="flex items-center gap-1.5 type-caption text-success mt-2"
                >
                  <Check className="w-3.5 h-3.5" /> 下载已开始，查看浏览器下载栏
                </motion.p>
              )}
              {error && (
                <motion.p
                  initial={{ opacity: 0, y: -2 }}
                  animate={{ opacity: 1, y: 0 }}
                  role="alert"
                  className="mt-2 flex items-center gap-1.5 type-caption text-danger"
                >
                  <AlertCircle className="w-3.5 h-3.5" /> {error}
                </motion.p>
              )}
            </AnimatePresence>
          </div>
        </div>
        <Button
          variant="secondary"
          size="md"
          onClick={onExport}
          disabled={busy}
          loading={busy}
          leftIcon={!busy ? <Download className="w-3.5 h-3.5" /> : undefined}
          className="w-full sm:w-auto whitespace-nowrap"
        >
          {busy ? "打包中" : "下载 zip"}
        </Button>
      </div>
    </section>
  );
}

// ———————————————————————————— 会话管理 ————————————————————————————

function SessionsSection() {
  const q = useMySessionsQuery();
  const revoke = useRevokeMySessionMutation();
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);

  const onRevoke = (s: SessionOut) => {
    setRevokeError(null);
    revoke.mutate(s.id, {
      onSettled: () => setPendingId(null),
      onError: (err) => {
        if (err instanceof ApiError) {
          setRevokeError(err.message || `操作失败 (HTTP ${err.status})`);
        } else {
          setRevokeError(err.message || "操作失败");
        }
      },
    });
  };

  const items = q.data?.items ?? [];

  return (
    <section className="space-y-3">
      <SectionHeader
        title="活跃会话"
        description={items.length > 0 ? `${items.length} 个设备登录中` : undefined}
      />
      <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 backdrop-blur-sm overflow-hidden">
        {q.isLoading ? (
          <div className="p-5 space-y-3">
            {SESSION_SKELETON_KEYS.map((key, i) => (
              <div
                key={key}
                className="h-14 rounded-[var(--radius-control)] bg-[var(--bg-2)] animate-pulse"
                style={{ animationDelay: `${i * 80}ms` }}
              />
            ))}
          </div>
        ) : q.isError ? (
          <div className="p-6 flex items-center justify-between gap-4 flex-wrap">
            <div className="flex items-start gap-3 min-w-0">
              <AlertCircle className="w-5 h-5 text-danger shrink-0 mt-0.5" />
              <div>
                <p className="type-body-sm text-[var(--danger-fg)]">加载失败</p>
                <p className="type-caption text-[var(--fg-2)] mt-1">
                  {q.error?.message ?? "未知错误"}
                </p>
              </div>
            </div>
            <Button
              variant="secondary"
              size="md"
              onClick={() => void q.refetch()}
              className="w-full sm:w-auto"
            >
              {copy.action.retry}
            </Button>
          </div>
        ) : items.length === 0 ? (
          <div className="p-10 text-center type-body-sm text-[var(--fg-2)]">
            {copy.state.empty}
          </div>
        ) : (
          <ul className="divide-y divide-[var(--border-subtle)]">
            {items.map((s, i) => (
              <SessionRow
                key={s.id}
                s={s}
                i={i}
                onActivate={() => setPendingId(s.id)}
                onCancel={() => setPendingId(null)}
                onConfirm={() => onRevoke(s)}
                confirming={pendingId === s.id}
                pending={revoke.isPending && pendingId === s.id}
              />
            ))}
          </ul>
        )}
      </div>
      {revokeError && (
        <p role="alert" className="flex items-center gap-1.5 type-body-sm text-[var(--danger-fg)]">
          <AlertCircle className="w-4 h-4" /> {revokeError}
        </p>
      )}
    </section>
  );
}

function SessionRow({
  s,
  i,
  onActivate,
  onCancel,
  onConfirm,
  confirming,
  pending,
}: {
  s: SessionOut;
  i: number;
  onActivate: () => void;
  onCancel: () => void;
  onConfirm: () => void;
  confirming: boolean;
  pending: boolean;
}) {
  const created = safeDistanceToNow(s.created_at);
  const expires = safeFormat(s.expires_at, "yyyy-MM-dd HH:mm");
  return (
    <motion.li
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, delay: Math.min(i * 0.03, 0.2) }}
      className="flex flex-col gap-3 px-4 py-4 transition-colors hover:bg-[var(--bg-2)]/50 sm:px-5 md:flex-row md:items-start md:justify-between md:gap-4"
    >
      <div className="min-w-0 flex-1 flex items-start gap-3">
        <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)]">
          <Monitor className="w-4 h-4" />
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="type-body-sm text-[var(--fg-0)] truncate">
              {s.ua ? truncate(s.ua, 80) : "未知设备"}
            </span>
            {s.is_current && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)] text-[10px] bg-accent-soft text-accent border border-accent-border">
                <span className="w-1 h-1 rounded-full bg-accent" />
                当前会话
              </span>
            )}
          </div>
          <div className="mt-1 type-caption text-[var(--fg-2)] font-mono tabular-nums flex flex-wrap gap-x-4 gap-y-0.5">
            <span>IP {s.ip ?? "—"}</span>
            <span>创建 {created}</span>
            <span>到期 {expires}</span>
          </div>
        </div>
      </div>
      {s.is_current ? (
        <span className="type-caption text-[var(--fg-2)] w-full md:w-auto md:self-center">—</span>
      ) : confirming ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="w-full md:w-auto inline-flex items-center gap-2 md:self-center"
        >
          <span className="type-caption text-[var(--fg-1)]">确认？</span>
          <Button
            variant="danger"
            size="sm"
            onClick={onConfirm}
            disabled={pending}
            loading={pending}
            className="flex-1 md:flex-none"
          >
            {pending ? "踢下线中" : "踢下线"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={onCancel}
            disabled={pending}
            className="flex-1 md:flex-none"
          >
            {copy.action.cancel}
          </Button>
        </motion.div>
      ) : (
        <Button
          variant="ghost"
          size="sm"
          onClick={onActivate}
          leftIcon={<LogOut className="w-3 h-3" />}
          className="w-full md:w-auto md:self-center text-danger hover:text-danger"
        >
          踢下线
        </Button>
      )}
    </motion.li>
  );
}

// ———————————————————————————— 危险区 ————————————————————————————

function DangerSection({
  email,
  loading,
}: {
  email: string | null;
  loading: boolean;
}) {
  const [confirmEmail, setConfirmEmail] = useState("");
  const [armed, setArmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const del = useDeleteMyAccountMutation();
  const queryClient = useQueryClient();

  // mounted 保护：mutation 解析后组件可能已卸载（用户回退/换页）。
  // 此时再 setError / location.assign 会浪费一次 setState 警告 + 不必要的 navigation。
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const matches = useMemo(
    () =>
      email != null &&
      email.length > 0 &&
      confirmEmail.trim().toLowerCase() === email.toLowerCase(),
    [email, confirmEmail],
  );

  const onDelete = () => {
    setError(null);
    if (!matches) return;
    del.mutate(undefined, {
      onSuccess: () => {
        if (!mountedRef.current) return;
        clearLocalAccountState(queryClient);
        if (typeof window !== "undefined") {
          window.location.assign("/login");
        }
      },
      onError: (err) => {
        if (!mountedRef.current) return;
        if (err instanceof ApiError) {
          setError(err.message || `删除失败 (HTTP ${err.status})`);
        } else {
          setError(err.message || "删除失败");
        }
      },
    });
  };

  return (
    <section className="space-y-3">
      <SectionHeader title="危险区" tone="danger" />
      <div className="rounded-[var(--radius-card)] border border-danger-border bg-danger-soft p-5 space-y-4">
        <div className="flex items-start gap-3">
          <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-danger-border bg-danger-soft text-danger">
            <AlertTriangle className="w-4 h-4" />
          </div>
          <div>
            <p className="type-body-sm text-[var(--fg-0)]">删除我的账号</p>
            <p className="type-caption text-[var(--fg-1)] mt-1 leading-relaxed">
              软删账号；所有登录会话立即失效，对话与图像会被标记为删除。保留期内可申请恢复，超后永久清除。
            </p>
          </div>
        </div>

        <div>
          <label
            htmlFor="confirm-email"
            className="block type-overline text-[var(--fg-1)] mb-1.5"
          >
            输入邮箱以确认
            {email && (
              <span className="ml-1 font-mono text-[var(--fg-1)] normal-case tracking-normal">
                ({email})
              </span>
            )}
          </label>
          <input
            id="confirm-email"
            type="email"
            value={confirmEmail}
            onChange={(e) => {
              setConfirmEmail(e.target.value);
              setArmed(false);
            }}
            placeholder={email ?? (loading ? copy.state.loading : "未登录")}
            disabled={!email || del.isPending}
            autoComplete="off"
            className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)]/72 px-3 text-base transition-colors placeholder:text-[var(--fg-2)] focus:border-danger-border focus:outline-none focus:ring-2 focus:ring-[var(--danger)]/20 disabled:opacity-50 md:h-9 md:text-sm"
          />
          {confirmEmail && !matches && (
            <p className="type-caption text-[var(--fg-2)] mt-1.5">
              邮箱不匹配
            </p>
          )}
        </div>

        {error && (
          <div role="alert" className="flex items-start gap-2 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-[var(--danger-fg)]">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            {error}
          </div>
        )}

        {!armed ? (
          <Button
            variant="danger"
            size="md"
            onClick={() => setArmed(true)}
            disabled={!matches || del.isPending}
            leftIcon={<Trash2 className="w-4 h-4" />}
            fullWidth
          >
            准备删除账号
          </Button>
        ) : (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            className="grid grid-cols-2 gap-2"
          >
            <Button
              variant="ghost"
              size="md"
              onClick={() => setArmed(false)}
              disabled={del.isPending}
            >
              {copy.action.cancel}
            </Button>
            <Button
              variant="danger"
              size="md"
              onClick={onDelete}
              disabled={!matches || del.isPending}
              loading={del.isPending}
              leftIcon={!del.isPending ? <Trash2 className="w-4 h-4" /> : undefined}
            >
              {del.isPending ? "删除中" : "永久删除"}
            </Button>
          </motion.div>
        )}
      </div>
    </section>
  );
}

// ———————————————————————————— helpers ————————————————————————————

const LOCAL_ACCOUNT_STORAGE_KEYS = [
  "lumen.haptic.enabled",
  "lumen.landscape-banner.dismissed",
] as const;

function removeKeys(storage: Storage, keys: readonly string[]): void {
  for (const key of keys) {
    storage.removeItem(key);
  }
}

function clearLocalAccountState(queryClient: QueryClient) {
  useChatStore.setState({
    currentUserId: null,
    currentConvId: null,
    messages: [],
    generations: {},
    imagesById: {},
    composerError: null,
    composer: {
      text: "",
      attachments: [],
      mode: "chat",
      params: {
        aspect_ratio: "7:10",
        size_mode: "auto",
        count: 1,
      },
      forceIntent: undefined,
      reasoningEffort: "high",
      fast: true,
      webSearch: true,
      fileSearch: false,
      codeInterpreter: false,
      imageGeneration: false,
      mask: null,
    },
  });
  useUiStore.setState({
    sidebarOpen: true,
    sidebarSearch: "",
    lightbox: {
      open: false,
      imageId: null,
      imageSrc: null,
      imagePreviewSrc: null,
      imageAlt: null,
      gallery: [],
      eventItems: null,
      action: null,
    },
    taskTray: {
      minimized: true,
    },
  });
  queryClient.clear();
  if (typeof window === "undefined") return;
  try {
    removeKeys(window.localStorage, LOCAL_ACCOUNT_STORAGE_KEYS);
  } catch {
    /* ignore */
  }
  try {
    removeKeys(window.sessionStorage, []);
  } catch {
    /* ignore */
  }
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function safeDistanceToNow(iso: string): string {
  try {
    return formatDistanceToNow(new Date(iso), {
      addSuffix: true,
      locale: zhCN,
    });
  } catch {
    return iso;
  }
}

function safeFormat(iso: string, pattern: string): string {
  try {
    return format(new Date(iso), pattern);
  } catch {
    return iso;
  }
}
