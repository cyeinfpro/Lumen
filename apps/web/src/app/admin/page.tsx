"use client";

// Lumen V1 管理面板。
// - 权限守卫：非 admin 显示占位 + replace("/")，避免内容闪烁
// - Tab：白名单 / 用户 / 邀请 / 系统设置（motion layoutId 丝滑指示器）
// - 白名单：内联搜索 + 内嵌删除确认 popover
// - 用户：搜索 + 角色过滤 + 表格（数字 tabular-nums）+ 加载更多
// - 子 panel 另见 _panels/*

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { format } from "date-fns";
import {
  AlertCircle,
  ArrowLeft,
  Inbox,
  Loader2,
  Search,
  ShieldCheck,
  UserCog,
  Users as UsersIcon,
} from "lucide-react";

import {
  useAddAllowedEmailMutation,
  useAdminUsersInfiniteQuery,
  useAllowedEmailsQuery,
  useRemoveAllowedEmailMutation,
} from "@/lib/queries";
import { ApiError, getMe, type AuthUser } from "@/lib/apiClient";
import { BackupsPanel } from "./_panels/BackupsPanel";
import { InvitesPanel } from "./_panels/InvitesPanel";
import { ProvidersPanel } from "./_panels/ProvidersPanel";
import { ProxiesPanel } from "./_panels/ProxiesPanel";
import { RequestEventsPanel } from "./_panels/RequestEventsPanel";
import { SettingsPanel } from "./_panels/SettingsPanel";
import { TelegramPanel } from "./_panels/TelegramPanel";

type MaybeAdminUser = AuthUser & { role?: "admin" | "member" };

type Tab =
  | "emails"
  | "users"
  | "events"
  | "invites"
  | "providers"
  | "proxies"
  | "telegram"
  | "settings"
  | "backups";

const TABS: { key: Tab; label: string }[] = [
  { key: "emails", label: "白名单" },
  { key: "users", label: "用户" },
  { key: "events", label: "请求事件" },
  { key: "invites", label: "邀请链接" },
  { key: "providers", label: "Provider" },
  { key: "proxies", label: "代理池" },
  { key: "telegram", label: "Telegram 机器人" },
  { key: "settings", label: "系统设置" },
  { key: "backups", label: "备份与恢复" },
];

const AUTH_STORAGE_KEYS = new Set([
  "lumen.auth",
  "lumen.session",
  "lumen.csrf",
  "csrf",
]);

function isAuthStorageEvent(e: StorageEvent): boolean {
  if (typeof window === "undefined") return false;
  if (e.storageArea !== window.localStorage) return false;
  if (e.key === null) return true;
  return AUTH_STORAGE_KEYS.has(e.key) || e.key.startsWith("lumen.auth.");
}

export default function AdminPage() {
  const router = useRouter();

  const meQuery = useQuery<MaybeAdminUser>({
    queryKey: ["me"],
    queryFn: () => getMe() as Promise<MaybeAdminUser>,
    retry: false,
  });

  const role = meQuery.data?.role;
  const isLoadingMe =
    meQuery.isLoading || (meQuery.isFetching && !meQuery.data);
  const refetchMe = meQuery.refetch;

  useEffect(() => {
    if (meQuery.isSuccess && role !== "admin") {
      router.replace("/");
    }
    if (meQuery.isError) {
      const err = meQuery.error;
      // 401：另一 tab 登出后再切回本 tab → 走 /login（保留 next 回到 admin）
      if (err instanceof ApiError && err.status === 401) {
        router.replace("/login?next=" + encodeURIComponent("/admin"));
      } else {
        router.replace("/");
      }
    }
  }, [meQuery.isSuccess, meQuery.isError, meQuery.error, role, router]);

  // 跨 tab 登出守卫：监听窗口 focus + storage 变化 → 主动 refetch /auth/me。
  // 单纯依赖 staleTime 可能让本 tab 长时间停留管理面板而身份失效却毫无察觉。
  useEffect(() => {
    if (typeof window === "undefined") return;
    const refresh = () => {
      void refetchMe();
    };
    const onStorage = (e: StorageEvent) => {
      if (isAuthStorageEvent(e)) refresh();
    };
    window.addEventListener("focus", refresh);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("focus", refresh);
      window.removeEventListener("storage", onStorage);
    };
  }, [refetchMe]);

  if (isLoadingMe) {
    return (
      <div className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-neutral-200">
        <div className="max-w-6xl mx-auto px-4 md:px-8 py-6 md:py-10 space-y-5">
          <div className="h-8 w-48 bg-white/5 rounded-lg animate-pulse" />
          <div className="h-4 w-64 bg-white/5 rounded animate-pulse" />
          <div className="h-10 w-80 bg-white/5 rounded-full animate-pulse mt-6" />
          <div className="h-72 w-full bg-white/5 rounded-2xl animate-pulse mt-4" />
        </div>
      </div>
    );
  }

  if (role !== "admin") {
    return (
      <div className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-neutral-300 flex items-center justify-center px-4">
        <div className="text-center space-y-3">
          <div className="mx-auto w-12 h-12 rounded-full bg-white/5 border border-white/10 flex items-center justify-center">
            <ShieldCheck className="w-5 h-5 text-neutral-400" />
          </div>
          <p className="text-lg">仅管理员可访问</p>
          <Link
            href="/"
            className="text-sm text-[var(--color-lumen-amber)] hover:underline mt-2 inline-block"
          >
            返回首页
          </Link>
        </div>
      </div>
    );
  }

  return <AdminInner me={meQuery.data} />;
}

function AdminInner({ me }: { me: MaybeAdminUser | undefined }) {
  const [tab, setTab] = useState<Tab>("emails");

  return (
    <motion.div
      initial={false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-neutral-200"
    >
      <div className="max-w-6xl mx-auto px-4 md:px-8 py-6 md:py-10">
        <header className="mb-6 md:mb-8 flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0">
            <h1 className="text-2xl md:text-3xl font-semibold tracking-tight">
              管理后台
            </h1>
            <p className="text-sm text-[var(--fg-1)] mt-1.5">
              管理用户、权限、系统配置
            </p>
          </div>
          <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
            {me?.email && (
              <div className="flex items-center gap-2 px-2.5 sm:px-3 py-1.5 rounded-full bg-white/5 border border-white/10 text-xs min-h-[32px]">
                <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-lumen-amber)] shadow-[0_0_8px_var(--color-lumen-amber)]" />
                <span className="text-neutral-300 truncate max-w-[140px] sm:max-w-[180px]">
                  {me.email}
                </span>
                <span className="px-1.5 py-0.5 rounded-md bg-[var(--color-lumen-amber)]/15 text-[var(--color-lumen-amber)] border border-[var(--color-lumen-amber)]/25 text-[10px] font-medium">
                  admin
                </span>
              </div>
            )}
            <Link
              href="/"
              className="inline-flex items-center gap-1.5 text-sm text-neutral-400 hover:text-neutral-100 transition-colors min-h-[44px] sm:min-h-0 px-2 sm:px-0"
            >
              <ArrowLeft className="w-4 h-4" />
              返回工作台
            </Link>
          </div>
        </header>

        <TabNav tab={tab} onChange={setTab} />

        <div className="mt-6">
          <AnimatePresence mode="wait">
            <motion.div
              key={tab}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.18, ease: "easeOut" }}
            >
              {tab === "emails" ? (
                <AllowedEmailsPanel />
              ) : tab === "users" ? (
                <UsersPanel />
              ) : tab === "events" ? (
                <RequestEventsPanel />
              ) : tab === "invites" ? (
                <InvitesPanel />
              ) : tab === "providers" ? (
                <ProvidersPanel />
              ) : tab === "proxies" ? (
                <ProxiesPanel />
              ) : tab === "telegram" ? (
                <TelegramPanel />
              ) : tab === "settings" ? (
                <SettingsPanel />
              ) : (
                <BackupsPanel />
              )}
            </motion.div>
          </AnimatePresence>
        </div>
      </div>
    </motion.div>
  );
}

function TabNav({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }) {
  return (
    <div className="overflow-x-auto scrollbar-thin -mx-4 px-4 md:mx-0 md:px-0 [-webkit-overflow-scrolling:touch]">
      <nav
        role="tablist"
        className="inline-flex items-center gap-1 p-1 rounded-full bg-white/[0.04] border border-white/10 backdrop-blur-sm"
      >
        {TABS.map((t) => {
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => onChange(t.key)}
              className="relative whitespace-nowrap shrink-0 px-3.5 md:px-4 py-1.5 text-xs md:text-sm rounded-full transition-colors"
            >
              {active && (
                <motion.span
                  layoutId="admin-tab-pill"
                  className="absolute inset-0 rounded-full bg-[var(--color-lumen-amber)] shadow-[0_6px_20px_-8px_var(--color-lumen-amber)]"
                  transition={{ type: "spring", stiffness: 400, damping: 34 }}
                />
              )}
              <span
                className={
                  "relative z-10 whitespace-nowrap " +
                  (active
                    ? "text-black font-medium"
                    : "text-neutral-300 hover:text-neutral-100")
                }
              >
                {t.label}
              </span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}

// ———————————————————— 白名单 ————————————————————

function AllowedEmailsPanel() {
  const q = useAllowedEmailsQuery();
  const [email, setEmail] = useState("");
  const [search, setSearch] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [pendingRemoveId, setPendingRemoveId] = useState<string | null>(null);

  const addMut = useAddAllowedEmailMutation({
    onSuccess: () => {
      setEmail("");
      setFormError(null);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setFormError("该邮箱已在白名单中");
      } else {
        setFormError(err.message || "添加失败");
      }
    },
  });

  const removeMut = useRemoveAllowedEmailMutation({
    onSettled: () => setPendingRemoveId(null),
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    const trimmed = email.trim();
    if (!trimmed) {
      setFormError("请输入邮箱");
      return;
    }
    addMut.mutate(trimmed);
  };

  const filtered = useMemo(() => {
    const rows = q.data?.items ?? [];
    const s = search.trim().toLowerCase();
    if (!s) return rows;
    return rows.filter(
      (r) =>
        r.email.toLowerCase().includes(s) ||
        (r.invited_by_email ?? "").toLowerCase().includes(s),
    );
  }, [q.data, search]);

  return (
    <section className="space-y-5">
      {/* —— 添加 / 搜索行 —— */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-white/10 rounded-2xl p-4 md:p-5 space-y-3">
        <form
          onSubmit={onSubmit}
          className="flex flex-col sm:flex-row gap-3 items-stretch sm:items-center"
        >
          <div className="flex-1 flex items-center gap-2 px-3 h-9 rounded-xl bg-[var(--bg-0)]/60 border border-white/10 focus-within:border-[var(--color-lumen-amber)]/50 focus-within:ring-2 focus-within:ring-[var(--color-lumen-amber)]/25 transition-colors">
            <label htmlFor="add-allowed-email" className="sr-only">
              邮箱
            </label>
            <input
              id="add-allowed-email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="someone@example.com"
              autoComplete="off"
              className="flex-1 bg-transparent text-sm focus:outline-none placeholder:text-neutral-600"
            />
          </div>
          {/* @hit-area-ok: admin desktop form submit button, desktop-only page */}
          <button
            type="submit"
            disabled={addMut.isPending}
            className="inline-flex items-center justify-center gap-1.5 h-9 px-4 rounded-xl bg-[var(--color-lumen-amber)] hover:brightness-110 active:scale-[0.97] text-black text-sm font-medium disabled:opacity-50 transition-all"
          >
            {addMut.isPending ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" /> 添加中
              </>
            ) : (
              "添加白名单"
            )}
          </button>
        </form>
        {formError && (
          <p className="flex items-center gap-1.5 text-xs text-red-300">
            <AlertCircle className="w-3.5 h-3.5" />
            {formError}
          </p>
        )}

        <div className="flex items-center gap-2 px-3 h-9 rounded-xl bg-[var(--bg-0)]/40 border border-white/8 focus-within:border-white/20 transition-colors">
          <Search className="w-3.5 h-3.5 text-neutral-500" />
          <label htmlFor="search-allowed" className="sr-only">
            搜索白名单
          </label>
          <input
            id="search-allowed"
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索邮箱或邀请人"
            className="flex-1 bg-transparent text-xs focus:outline-none placeholder:text-neutral-600"
          />
        </div>
      </div>

      {/* —— 列表 —— */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-white/10 rounded-2xl overflow-hidden">
        {q.isLoading ? (
          <ListSkeleton rows={4} />
        ) : q.isError ? (
          <ErrorBlock
            message={q.error?.message ?? "未知错误"}
            onRetry={() => void q.refetch()}
          />
        ) : filtered.length === 0 ? (
          <EmptyBlock
            title={search ? "没有匹配结果" : "白名单为空"}
            description={
              search
                ? "试试换个关键词"
                : "添加邮箱允许该用户注册 Lumen"
            }
          />
        ) : (
          <>
            {/* 桌面端表格 */}
            <div className="hidden md:block overflow-x-auto [-webkit-overflow-scrolling:touch]">
              <table className="w-full text-sm">
                <thead className="text-xs uppercase tracking-wider text-[var(--fg-1)] border-b border-white/10">
                  <tr>
                    <th className="text-left py-3 px-4 font-medium">Email</th>
                    <th className="text-left py-3 px-4 font-medium">邀请人</th>
                    <th className="text-left py-3 px-4 font-medium">创建时间</th>
                    <th className="text-right py-3 px-4 font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((row, i) => (
                    <motion.tr
                      key={row.id}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{
                        duration: 0.18,
                        delay: Math.min(i * 0.03, 0.18),
                      }}
                      className="border-t border-white/5 hover:bg-white/[0.03] transition-colors align-middle"
                    >
                      <td className="py-3 px-4 text-neutral-100 break-all">{row.email}</td>
                      <td className="py-3 px-4 text-neutral-400 break-all">
                        {row.invited_by_email ?? "—"}
                      </td>
                      <td className="py-3 px-4 text-neutral-400 font-mono text-xs tabular-nums whitespace-nowrap">
                        {formatISODate(row.created_at)}
                      </td>
                      <td className="py-3 px-4 text-right">
                        <ConfirmInlineRemove
                          pending={
                            removeMut.isPending && pendingRemoveId === row.id
                          }
                          active={pendingRemoveId === row.id}
                          onActivate={() => setPendingRemoveId(row.id)}
                          onCancel={() => setPendingRemoveId(null)}
                          onConfirm={() => removeMut.mutate(row.id)}
                          email={row.email}
                        />
                      </td>
                    </motion.tr>
                  ))}
                </tbody>
              </table>
            </div>
            {/* 移动端卡片列表 */}
            <ul className="md:hidden divide-y divide-white/5">
              {filtered.map((row) => (
                <li key={row.id} className="p-4 space-y-2">
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm text-neutral-100 break-all min-w-0">
                      {row.email}
                    </span>
                    <ConfirmInlineRemove
                      pending={
                        removeMut.isPending && pendingRemoveId === row.id
                      }
                      active={pendingRemoveId === row.id}
                      onActivate={() => setPendingRemoveId(row.id)}
                      onCancel={() => setPendingRemoveId(null)}
                      onConfirm={() => removeMut.mutate(row.id)}
                      email={row.email}
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-neutral-500">
                        邀请人
                      </div>
                      <div className="text-neutral-300 break-all">
                        {row.invited_by_email ?? "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-neutral-500">
                        创建
                      </div>
                      <div className="text-neutral-300 font-mono tabular-nums">
                        {formatISODate(row.created_at)}
                      </div>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </section>
  );
}

function ConfirmInlineRemove({
  pending,
  active,
  onActivate,
  onCancel,
  onConfirm,
  email,
}: {
  pending: boolean;
  active: boolean;
  onActivate: () => void;
  onCancel: () => void;
  onConfirm: () => void;
  email: string;
}) {
  if (!active) {
    return (
      <button
        type="button"
        onClick={onActivate}
        className="shrink-0 inline-flex items-center justify-center min-h-[32px] px-2.5 text-xs text-red-300 hover:text-red-200 transition-colors"
        aria-label={`移除 ${email}`}
      >
        移除
      </button>
    );
  }
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.96 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.14 }}
      className="inline-flex items-center gap-2 shrink-0"
    >
      <span className="text-xs text-neutral-400 hidden sm:inline">确认?</span>
      <button
        type="button"
        onClick={onConfirm}
        disabled={pending}
        className="text-xs px-3 py-1.5 min-h-[32px] rounded-md bg-red-500/80 hover:bg-red-500 text-white disabled:opacity-50 transition-colors"
      >
        {pending ? "移除中" : "移除"}
      </button>
      <button
        type="button"
        onClick={onCancel}
        disabled={pending}
        className="text-xs px-3 py-1.5 min-h-[32px] rounded-md bg-white/5 hover:bg-white/10 text-neutral-300 disabled:opacity-50 transition-colors"
      >
        取消
      </button>
    </motion.div>
  );
}

// ———————————————————— 用户 ————————————————————

function UsersPanel() {
  const PAGE_SIZE = 50;
  const q = useAdminUsersInfiniteQuery({ limit: PAGE_SIZE });

  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState<"all" | "admin" | "member">(
    "all",
  );

  const rows = useMemo(
    () => q.data?.pages.flatMap((p) => p.items) ?? [],
    [q.data],
  );

  const filtered = useMemo(() => {
    const s = search.trim().toLowerCase();
    return rows.filter((u) => {
      if (roleFilter !== "all" && u.role !== roleFilter) return false;
      if (!s) return true;
      return (
        u.email.toLowerCase().includes(s) ||
        (u.display_name ?? "").toLowerCase().includes(s)
      );
    });
  }, [rows, search, roleFilter]);

  return (
    <section className="space-y-5">
      {/* —— 过滤行 —— */}
      <div className="flex flex-col md:flex-row gap-3 md:items-center">
        <div className="flex-1 w-full md:min-w-[220px] flex items-center gap-2 px-3 h-9 rounded-xl bg-[var(--bg-0)]/60 border border-white/10 focus-within:border-[var(--color-lumen-amber)]/50 focus-within:ring-2 focus-within:ring-[var(--color-lumen-amber)]/25 transition-colors">
          <Search className="w-3.5 h-3.5 text-neutral-500" />
          <label htmlFor="search-users" className="sr-only">
            搜索用户
          </label>
          <input
            id="search-users"
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索邮箱或名称"
            className="flex-1 bg-transparent text-sm focus:outline-none placeholder:text-neutral-600"
          />
        </div>
        <div
          role="tablist"
          aria-label="按角色过滤"
          className="inline-flex items-center gap-0.5 p-0.5 rounded-xl bg-white/[0.04] border border-white/10 text-xs"
        >
          {(["all", "admin", "member"] as const).map((r) => (
            <button
              key={r}
              role="tab"
              aria-selected={roleFilter === r}
              type="button"
              onClick={() => setRoleFilter(r)}
              className={
                "px-3 h-8 sm:h-7 rounded-lg transition-colors " +
                (roleFilter === r
                  ? "bg-white/10 text-neutral-100"
                  : "text-neutral-400 hover:text-neutral-100")
              }
            >
              {r === "all" ? "全部" : r}
            </button>
          ))}
        </div>
      </div>

      {/* —— 表格 —— */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-white/10 rounded-2xl overflow-hidden">
        {q.isLoading && rows.length === 0 ? (
          <ListSkeleton rows={6} />
        ) : q.isError && rows.length === 0 ? (
          <ErrorBlock
            message={q.error?.message ?? "未知错误"}
            onRetry={() => void q.refetch()}
          />
        ) : filtered.length === 0 ? (
          <EmptyBlock
            title={rows.length === 0 ? "暂无用户" : "没有匹配结果"}
            description={
              rows.length === 0
                ? "注册的用户会出现在这里"
                : "试试切换角色或换个关键词"
            }
          />
        ) : (
          <>
            {/* 桌面端表格 */}
            <div className="hidden md:block overflow-x-auto [-webkit-overflow-scrolling:touch]">
              <table className="w-full text-sm">
                <thead className="text-xs uppercase tracking-wider text-[var(--fg-1)] border-b border-white/10">
                  <tr>
                    <th className="text-left py-3 px-4 font-medium">Email</th>
                    <th className="text-left py-3 px-4 font-medium">角色</th>
                    <th className="text-left py-3 px-4 font-medium">名称</th>
                    <th className="text-left py-3 px-4 font-medium">注册</th>
                    <th className="text-right py-3 px-4 font-medium">生成</th>
                    <th className="text-right py-3 px-4 font-medium">对话</th>
                    <th className="text-right py-3 px-4 font-medium">消息</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((u, i) => (
                    <motion.tr
                      key={u.id}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{
                        duration: 0.18,
                        delay: Math.min(i * 0.02, 0.2),
                      }}
                      className="border-t border-white/5 hover:bg-white/[0.03] transition-colors"
                    >
                      <td className="py-3 px-4 text-neutral-100 break-all">{u.email}</td>
                      <td className="py-3 px-4">
                        <RoleBadge role={u.role} />
                      </td>
                      <td className="py-3 px-4 text-neutral-300 break-all">
                        {u.display_name ?? "—"}
                      </td>
                      <td className="py-3 px-4 text-neutral-400 font-mono text-xs tabular-nums whitespace-nowrap">
                        {formatISODate(u.created_at)}
                      </td>
                      <td className="py-3 px-4 text-right text-neutral-100 font-mono tabular-nums">
                        {u.generations_count}
                      </td>
                      <td className="py-3 px-4 text-right text-neutral-100 font-mono tabular-nums">
                        {u.completions_count}
                      </td>
                      <td className="py-3 px-4 text-right text-neutral-100 font-mono tabular-nums">
                        {u.messages_count}
                      </td>
                    </motion.tr>
                  ))}
                </tbody>
              </table>
            </div>
            {/* 移动端卡片列表 */}
            <ul className="md:hidden divide-y divide-white/5">
              {filtered.map((u) => (
                <li key={u.id} className="p-4 space-y-2">
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm text-neutral-100 break-all min-w-0 flex-1">
                      {u.email}
                    </span>
                    <div className="shrink-0">
                      <RoleBadge role={u.role} />
                    </div>
                  </div>
                  {u.display_name && (
                    <div className="text-xs text-neutral-400 break-all">
                      {u.display_name}
                    </div>
                  )}
                  <div className="text-sm text-neutral-500 font-mono tabular-nums">
                    {formatISODate(u.created_at)}
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-xs">
                    <MiniStat label="生成" value={u.generations_count} />
                    <MiniStat label="对话" value={u.completions_count} />
                    <MiniStat label="消息" value={u.messages_count} />
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      {q.hasNextPage && (
        <div className="flex justify-center">
          <button
            type="button"
            onClick={() => void q.fetchNextPage()}
            disabled={q.isFetchingNextPage}
            className="inline-flex items-center gap-1.5 h-9 px-5 rounded-xl bg-white/[0.06] hover:bg-white/[0.1] border border-white/10 text-sm disabled:opacity-50 transition-colors"
          >
            {q.isFetchingNextPage ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" /> 加载中
              </>
            ) : (
              "加载更多"
            )}
          </button>
        </div>
      )}
    </section>
  );
}

function MiniStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg bg-white/[0.03] border border-white/5 px-2 py-1.5">
      <div className="text-[11px] uppercase tracking-wider text-neutral-500">
        {label}
      </div>
      <div className="text-base text-neutral-100 font-mono tabular-nums">
        {value}
      </div>
    </div>
  );
}

// ———————————————————— shared ————————————————————

export function ListSkeleton({ rows = 5 }: { rows?: number }) {
  const keys = Array.from(
    { length: rows },
    (_, i) => `admin-list-skeleton-${i + 1}`,
  );

  return (
    <div className="p-4 space-y-3">
      {keys.map((key, i) => (
        <div
          key={key}
          className="flex items-center gap-3 animate-pulse"
          style={{ animationDelay: `${i * 60}ms` }}
        >
          <div className="h-4 w-1/3 bg-white/5 rounded" />
          <div className="h-4 w-16 bg-white/5 rounded" />
          <div className="h-4 flex-1 bg-white/5 rounded" />
          <div className="h-4 w-20 bg-white/5 rounded" />
        </div>
      ))}
    </div>
  );
}

export function EmptyBlock({
  title,
  description,
  cta,
}: {
  title: string;
  description?: string;
  cta?: React.ReactNode;
}) {
  return (
    <div className="py-14 flex flex-col items-center gap-3 text-center">
      <div className="w-12 h-12 rounded-2xl bg-white/5 border border-white/10 flex items-center justify-center">
        <Inbox className="w-5 h-5 text-neutral-500" />
      </div>
      <div>
        <p className="text-sm text-neutral-200">{title}</p>
        {description && (
          <p className="text-xs text-neutral-500 mt-1">{description}</p>
        )}
      </div>
      {cta}
    </div>
  );
}

export function ErrorBlock({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="p-6 flex items-center justify-between gap-4 rounded-2xl border border-red-500/30 bg-red-500/5">
      <div className="flex items-start gap-3">
        <AlertCircle className="w-5 h-5 text-red-300 shrink-0 mt-0.5" />
        <div>
          <p className="text-sm text-red-200">加载失败</p>
          <p className="text-xs text-neutral-400 mt-1">{message}</p>
        </div>
      </div>
      {onRetry && (
        /* @hit-area-ok: admin desktop error retry button, desktop-only page */
        <button
          type="button"
          onClick={onRetry}
          className="h-8 px-3 rounded-lg bg-white/10 hover:bg-white/15 border border-white/15 text-xs transition-colors"
        >
          重试
        </button>
      )}
    </div>
  );
}

function RoleBadge({ role }: { role: "admin" | "member" }) {
  if (role === "admin") {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs bg-[var(--color-lumen-amber)]/15 text-[var(--color-lumen-amber)] border border-[var(--color-lumen-amber)]/30">
        <UserCog className="w-3 h-3" />
        admin
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs bg-white/5 text-neutral-400 border border-white/10">
      <UsersIcon className="w-3 h-3" />
      member
    </span>
  );
}

function formatISODate(s: string): string {
  try {
    return format(new Date(s), "yyyy-MM-dd HH:mm");
  } catch {
    return s;
  }
}
