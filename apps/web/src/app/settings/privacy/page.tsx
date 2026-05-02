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
  Loader2,
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
    <motion.div
      initial={false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-neutral-200"
    >
      <div className="max-w-4xl mx-auto px-4 md:px-8 py-6 md:py-10 space-y-8 safe-x mobile-compact">
        <header className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl md:text-3xl font-semibold tracking-tight">
              隐私 & 数据
            </h1>
            <p className="text-sm text-[var(--fg-1)] mt-1.5">
              查看数据、登录会话和账号删除选项。
            </p>
          </div>
          <Link
            href="/me"
            className="inline-flex items-center gap-1.5 text-sm text-neutral-400 hover:text-neutral-100 transition-colors whitespace-nowrap"
          >
            <ArrowLeft className="w-4 h-4" />
            返回我的
          </Link>
        </header>

        <ExportSection />
        <SessionsSection />
        <DangerSection email={me.data?.email ?? null} loading={me.isLoading} />
      </div>
    </motion.div>
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
          "text-xs font-medium uppercase tracking-wider " +
          (tone === "danger" ? "text-red-300/90" : "text-[var(--fg-1)]")
        }
      >
        {title}
      </h2>
      <div className="flex-1 h-px bg-white/8" />
      {description && (
        <p className="text-xs text-neutral-500">{description}</p>
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
      <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 backdrop-blur-sm p-5 flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-start gap-3 flex-1 min-w-[200px]">
          <div className="shrink-0 w-9 h-9 rounded-xl bg-white/5 border border-white/10 flex items-center justify-center text-neutral-300">
            <Download className="w-4 h-4" />
          </div>
          <div>
            <p className="text-sm text-neutral-100">打包下载你的全部数据</p>
            <p className="text-xs text-neutral-500 mt-1">
              包含对话、消息和图像引用，输出为 zip（数秒至数十秒）。
            </p>
            <AnimatePresence>
              {doneAt && (
                <motion.p
                  initial={{ opacity: 0, y: -2 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  className="flex items-center gap-1.5 text-xs text-emerald-300 mt-2"
                >
                  <Check className="w-3.5 h-3.5" /> 下载已开始，请检查浏览器下载栏
                </motion.p>
              )}
              {error && (
                <motion.p
                  initial={{ opacity: 0, y: -2 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="flex items-center gap-1.5 text-xs text-red-300 mt-2"
                >
                  <AlertCircle className="w-3.5 h-3.5" /> {error}
                </motion.p>
              )}
            </AnimatePresence>
          </div>
        </div>
        <button
          type="button"
          onClick={onExport}
          disabled={busy}
          className="inline-flex items-center justify-center gap-1.5 h-11 sm:h-9 w-full sm:w-auto px-4 rounded-xl bg-white/10 hover:bg-white/15 border border-white/15 text-sm disabled:opacity-50 whitespace-nowrap transition-colors"
        >
          {busy ? (
            <>
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> 打包中
            </>
          ) : (
            <>
              <Download className="w-3.5 h-3.5" /> 下载 zip
            </>
          )}
        </button>
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
      <div className="rounded-2xl border border-white/10 bg-[var(--bg-1)]/60 backdrop-blur-sm overflow-hidden">
        {q.isLoading ? (
          <div className="p-5 space-y-3">
            {SESSION_SKELETON_KEYS.map((key, i) => (
              <div
                key={key}
                className="h-14 rounded-xl bg-white/5 animate-pulse"
                style={{ animationDelay: `${i * 80}ms` }}
              />
            ))}
          </div>
        ) : q.isError ? (
          <div className="p-6 flex items-center justify-between gap-4 flex-wrap">
            <div className="flex items-start gap-3 min-w-0">
              <AlertCircle className="w-5 h-5 text-red-300 shrink-0 mt-0.5" />
              <div>
                <p className="text-sm text-red-200">加载失败</p>
                <p className="text-xs text-neutral-400 mt-1">
                  {q.error?.message ?? "未知错误"}
                </p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => void q.refetch()}
              className="h-11 sm:h-9 w-full sm:w-auto px-4 rounded-xl bg-white/10 hover:bg-white/15 border border-white/15 text-sm transition-colors"
            >
              重试
            </button>
          </div>
        ) : items.length === 0 ? (
          <div className="p-10 text-center text-sm text-neutral-500">
            没有活跃会话
          </div>
        ) : (
          <ul className="divide-y divide-white/5">
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
        <p className="flex items-center gap-1.5 text-sm text-red-300">
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
      className="px-5 py-4 flex flex-col md:flex-row md:items-start md:justify-between gap-3 md:gap-4 hover:bg-white/[0.02] transition-colors"
    >
      <div className="min-w-0 flex-1 flex items-start gap-3">
        <div className="shrink-0 w-8 h-8 rounded-lg bg-white/5 border border-white/10 flex items-center justify-center text-neutral-400">
          <Monitor className="w-4 h-4" />
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm text-neutral-100 truncate">
              {s.ua ? truncate(s.ua, 80) : "未知设备"}
            </span>
            {s.is_current && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] bg-[var(--color-lumen-amber)]/15 text-[var(--color-lumen-amber)] border border-[var(--color-lumen-amber)]/30">
                <span className="w-1 h-1 rounded-full bg-[var(--color-lumen-amber)]" />
                当前会话
              </span>
            )}
          </div>
          <div className="mt-1 text-xs text-neutral-500 font-mono tabular-nums flex flex-wrap gap-x-4 gap-y-0.5">
            <span>IP {s.ip ?? "—"}</span>
            <span>创建 {created}</span>
            <span>到期 {expires}</span>
          </div>
        </div>
      </div>
      {s.is_current ? (
        <span className="text-xs text-neutral-600 w-full md:w-auto md:self-center">—</span>
      ) : confirming ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.95 }}
          animate={{ opacity: 1, scale: 1 }}
          className="w-full md:w-auto inline-flex items-center gap-2 md:self-center"
        >
          <span className="text-xs text-neutral-400">确认?</span>
          <button
            type="button"
            onClick={onConfirm}
            disabled={pending}
            aria-disabled={pending}
            className="flex-1 md:flex-none h-7 px-2.5 rounded-md bg-red-500/80 hover:bg-red-500 text-white text-xs disabled:opacity-50 transition-colors"
          >
            {pending ? "踢下线中" : "踢下线"}
          </button>
          <button
            type="button"
            onClick={onCancel}
            disabled={pending}
            aria-disabled={pending}
            className="flex-1 md:flex-none h-7 px-2.5 rounded-md bg-white/5 hover:bg-white/10 text-neutral-300 text-xs disabled:opacity-50 transition-colors"
          >
            取消
          </button>
        </motion.div>
      ) : (
        /* @hit-area-ok: settings page button, md:h-auto collapses to text-link on desktop, mobile h-9 acceptable in settings context */
        <button
          type="button"
          onClick={onActivate}
          className="w-full md:w-auto inline-flex items-center justify-center gap-1 md:self-center text-xs text-red-300 hover:text-red-200 transition-colors h-9 md:h-auto rounded-md bg-white/5 md:bg-transparent border border-white/10 md:border-0 md:p-0"
        >
          <LogOut className="w-3 h-3" />
          踢下线
        </button>
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
      <div className="rounded-2xl border border-red-500/30 bg-red-500/[0.04] p-5 space-y-4">
        <div className="flex items-start gap-3">
          <div className="shrink-0 w-9 h-9 rounded-xl bg-red-500/10 border border-red-500/25 text-red-300 flex items-center justify-center">
            <AlertTriangle className="w-4 h-4" />
          </div>
          <div>
            <p className="text-sm text-neutral-100">删除我的账号</p>
            <p className="text-xs text-neutral-400 mt-1 leading-relaxed">
              软删账号；所有登录会话立即失效，对话与图像会被标记为删除。保留期内可申请恢复，超过后将被永久清除。
            </p>
          </div>
        </div>

        <div>
          <label
            htmlFor="confirm-email"
            className="block text-[11px] font-medium uppercase tracking-wider text-[var(--fg-1)] mb-1.5"
          >
            输入你的邮箱以确认
            {email && (
              <span className="ml-1 font-mono text-neutral-400 normal-case tracking-normal">
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
            placeholder={email ?? (loading ? "加载中…" : "请先登录")}
            disabled={!email || del.isPending}
            autoComplete="off"
            className="w-full h-9 px-3 rounded-xl bg-black/30 border border-white/10 text-sm focus:outline-none focus:border-red-400/60 focus:ring-2 focus:ring-red-400/20 placeholder:text-neutral-600 disabled:opacity-50 transition-colors"
          />
          {confirmEmail && !matches && (
            <p className="text-xs text-neutral-500 mt-1.5">
              邮箱与当前账号不匹配
            </p>
          )}
        </div>

        {error && (
          <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300 flex items-start gap-2">
            <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
            {error}
          </div>
        )}

        {!armed ? (
          <button
            type="button"
            onClick={() => setArmed(true)}
            disabled={!matches || del.isPending}
            className="w-full inline-flex items-center justify-center gap-1.5 h-10 px-5 rounded-xl bg-red-500/80 hover:bg-red-500 text-white text-sm font-medium disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
          >
            <Trash2 className="w-4 h-4" /> 准备删除账号
          </button>
        ) : (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            className="grid grid-cols-2 gap-2"
          >
            <button
              type="button"
              onClick={() => setArmed(false)}
              disabled={del.isPending}
              className="h-10 rounded-xl bg-white/5 hover:bg-white/10 border border-white/10 text-sm disabled:opacity-50 transition-colors"
            >
              取消
            </button>
            {/* @hit-area-ok: settings danger zone confirm button, h-10 acceptable in desktop settings context */}
            <button
              type="button"
              onClick={onDelete}
              disabled={!matches || del.isPending}
              className="h-10 inline-flex items-center justify-center gap-1.5 rounded-xl bg-red-500 hover:brightness-110 active:scale-[0.97] text-white text-sm font-medium disabled:opacity-40 transition-all"
            >
              {del.isPending ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" /> 删除中
                </>
              ) : (
                <>
                  <Trash2 className="w-4 h-4" /> 永久删除
                </>
              )}
            </button>
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
        aspect_ratio: "16:9",
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
