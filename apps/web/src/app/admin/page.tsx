"use client";

// Lumen V1 管理面板。
// - 权限守卫：非 admin 显示占位 + replace("/")，避免内容闪烁
// - Tab：白名单 / 用户 / 邀请 / 系统设置（motion layoutId 丝滑指示器）
// - 白名单：内联搜索 + 内嵌删除确认 popover
// - 用户：搜索 + 角色过滤 + 表格（数字 tabular-nums）+ 加载更多
// - 子 panel 另见 _panels/*

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import NextImage from "next/image";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  Activity,
  AlertCircle,
  ArrowLeft,
  Archive,
  Clapperboard,
  CreditCard,
  Eye,
  HardDrive,
  Images,
  KeyRound,
  Link2,
  Loader2,
  MailCheck,
  MessageCircle,
  Search,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  Trash2,
  UserCog,
  Users as UsersIcon,
  Wifi,
  X,
  type LucideIcon,
} from "lucide-react";

import {
  useAdminUserHistoryQuery,
  useAdminUsersInfiniteQuery,
  useDeleteAdminUserMutation,
  useSetAdminUserPasswordMutation,
} from "@/lib/queries";
import { ApiError, getMe, type AuthUser } from "@/lib/apiClient";
import type { AdminUserOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";
import { BackupsPanel } from "./_panels/BackupsPanel";
import { InvitesPanel } from "./_panels/InvitesPanel";
import { ByokPanel } from "./_panels/ByokPanel";
import { BillingPanel } from "./_panels/BillingPanel";
import { HealthPanel } from "./_panels/HealthPanel";
import { ProvidersPanel } from "./_panels/ProvidersPanel";
import { ProxiesPanel } from "./_panels/ProxiesPanel";
import { RequestEventsPanel } from "./_panels/RequestEventsPanel";
import { SettingsPanel } from "./_panels/SettingsPanel";
import { StoragePanel } from "./_panels/StoragePanel";
import { TelegramPanel } from "./_panels/TelegramPanel";
import { VideoProvidersPanel } from "./_panels/VideoProvidersPanel";
import { AllowedEmailsPanel } from "./_components/AllowedEmailsPanel";
import {
  EmptyBlock,
  ErrorBlock,
  ListSkeleton,
} from "./_components/AdminFeedback";
import {
  adminInputShellClassName,
  formatISODate,
  tableShellClassName,
} from "./_components/adminUi";
import adminMobileStyles from "./admin-mobile.module.css";

type MaybeAdminUser = AuthUser & { role?: "admin" | "member" };

type Tab =
  | "health"
  | "emails"
  | "users"
  | "events"
  | "invites"
  | "byok"
  | "billing"
  | "providers"
  | "video_providers"
  | "proxies"
  | "telegram"
  | "settings"
  | "storage"
  | "backups";

type TabGroup = "overview" | "access" | "operations" | "infrastructure";

type TabMeta = {
  key: Tab;
  group: TabGroup;
  label: string;
  title: string;
  description: string;
  icon: LucideIcon;
};

type UserRoleFilter = "all" | "admin" | "member";

const TAB_GROUPS: {
  key: TabGroup;
  label: string;
  description: string;
}[] = [
  {
    key: "overview",
    label: "总览",
    description: "先看风险，再进细节",
  },
  {
    key: "access",
    label: "访问与用户",
    description: "账号、邀请、费用与自带 Key",
  },
  {
    key: "operations",
    label: "运行与审计",
    description: "请求、供应商、代理与机器人",
  },
  {
    key: "infrastructure",
    label: "系统与数据",
    description: "配置、存储、备份与恢复",
  },
];

const TABS: TabMeta[] = [
  {
    key: "health",
    group: "overview",
    label: "健康",
    title: "健康总览",
    description: "集中查看供应商、代理、计费、Telegram 和错误样本。",
    icon: Activity,
  },
  {
    key: "emails",
    group: "access",
    label: "白名单",
    title: "注册白名单",
    description: "允许指定邮箱注册，并追踪邀请来源。",
    icon: MailCheck,
  },
  {
    key: "users",
    group: "access",
    label: "用户",
    title: "用户与用量",
    description: "按角色筛选用户，查看生成、对话和消息统计。",
    icon: UsersIcon,
  },
  {
    key: "invites",
    group: "access",
    label: "邀请链接",
    title: "邀请链接",
    description: "生成、复制和撤销面向新用户的邀请链接。",
    icon: Link2,
  },
  {
    key: "byok",
    group: "access",
    label: "API 站接入",
    title: "API 站接入",
    description: "管理用户自带 API Key 的接入、验证和降级策略。",
    icon: KeyRound,
  },
  {
    key: "billing",
    group: "access",
    label: "计费",
    title: "计费与兑换",
    description: "检查余额、价格、兑换码和异常资金占用。",
    icon: CreditCard,
  },
  {
    key: "events",
    group: "operations",
    label: "请求事件",
    title: "请求事件",
    description: "排查请求失败、上游 attempt 和用户侧异常。",
    icon: ShieldCheck,
  },
  {
    key: "providers",
    group: "operations",
    label: "供应商",
    title: "供应商路由",
    description: "配置模型供应商、探活、优先级和图片任务能力。",
    icon: Server,
  },
  {
    key: "video_providers",
    group: "operations",
    label: "视频供应商",
    title: "AI 视频供应商",
    description: "配置 Seedance/Veo 视频任务供应商、模型映射、代理和并发。",
    icon: Clapperboard,
  },
  {
    key: "proxies",
    group: "operations",
    label: "代理池",
    title: "代理池",
    description: "维护出站代理，给供应商、Telegram 和更新流程使用。",
    icon: Wifi,
  },
  {
    key: "telegram",
    group: "operations",
    label: "Telegram",
    title: "Telegram 机器人",
    description: "配置机器人 token、用户白名单和代理策略。",
    icon: MessageCircle,
  },
  {
    key: "settings",
    group: "infrastructure",
    label: "系统设置",
    title: "系统设置",
    description: "用更直白的方式调整生图、上游、长对话和更新参数。",
    icon: SlidersHorizontal,
  },
  {
    key: "storage",
    group: "infrastructure",
    label: "存储后端",
    title: "存储后端",
    description: "切换本地或 SMB 存储，测试连接并应用配置。",
    icon: HardDrive,
  },
  {
    key: "backups",
    group: "infrastructure",
    label: "备份恢复",
    title: "备份与恢复",
    description: "查看自动备份、手动备份，并在必要时恢复快照。",
    icon: Archive,
  },
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

function adminAuthRedirectPath(error: unknown): string | null {
  if (
    error instanceof ApiError &&
    (error.status === 401 || error.status === 403)
  ) {
    return "/login?next=" + encodeURIComponent("/admin");
  }
  return null;
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
  const refreshMe = useCallback(() => {
    void refetchMe();
  }, [refetchMe]);

  useEffect(() => {
    if (meQuery.isSuccess && role !== "admin") {
      router.replace("/");
    }
    if (meQuery.isError) {
      const redirectPath = adminAuthRedirectPath(meQuery.error);
      if (redirectPath) router.replace(redirectPath);
    }
  }, [meQuery.isSuccess, meQuery.isError, meQuery.error, role, router]);

  // 跨 tab 登出守卫：监听窗口 focus + storage 变化 → 主动 refetch /auth/me。
  // 单纯依赖 staleTime 可能让本 tab 长时间停留管理面板而身份失效却毫无察觉。
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onStorage = (e: StorageEvent) => {
      if (isAuthStorageEvent(e)) refreshMe();
    };
    window.addEventListener("focus", refreshMe);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("focus", refreshMe);
      window.removeEventListener("storage", onStorage);
    };
  }, [refreshMe]);

  if (isLoadingMe) {
    return (
      <div className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-[var(--fg-0)]">
        <div className="max-w-6xl mx-auto px-4 md:px-8 py-6 md:py-10 space-y-5">
          <div className="h-8 w-48 animate-pulse rounded-[var(--radius-card)] bg-[var(--bg-1)]" />
          <div className="h-4 w-64 animate-pulse rounded-[var(--radius-control)] bg-[var(--bg-1)]" />
          <div className="mt-6 h-10 w-full max-w-80 animate-pulse rounded-full bg-[var(--bg-1)]" />
          <div className="mt-4 h-72 w-full animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-1)]" />
        </div>
      </div>
    );
  }

  if (meQuery.isError) {
    const redirectPath = adminAuthRedirectPath(meQuery.error);
    if (redirectPath) {
      return <AdminAccessPending message="登录状态已失效，正在跳转登录…" />;
    }
    return (
      <AdminAccessError
        onRetry={refreshMe}
        pending={meQuery.isFetching}
      />
    );
  }

  if (role !== "admin") {
    return (
      <div className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-[var(--fg-1)] flex items-center justify-center px-4">
        <div className="text-center space-y-3">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--bg-1)]">
            <ShieldCheck className="w-5 h-5 text-[var(--fg-2)]" />
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

function AdminAccessPending({ message }: { message: string }) {
  return (
    <div className="flex min-h-[100dvh] w-full flex-1 items-center justify-center bg-[var(--bg-0)] px-4 text-[var(--fg-1)]">
      <div role="status" className="flex items-center gap-2 text-sm">
        <Loader2 className="h-4 w-4 animate-spin" />
        {message}
      </div>
    </div>
  );
}

function AdminAccessError({
  onRetry,
  pending,
}: {
  onRetry: () => void;
  pending: boolean;
}) {
  return (
    <div className="flex min-h-[100dvh] w-full flex-1 items-center justify-center bg-[var(--bg-0)] px-4 text-[var(--fg-0)]">
      <div className="w-full max-w-sm rounded-[var(--radius-panel)] border border-danger-border bg-danger-soft p-5 text-center">
        <AlertCircle className="mx-auto h-6 w-6 text-danger" />
        <h1 className="mt-3 type-card-title">无法验证管理员身份</h1>
        <p className="mt-1.5 type-body-sm text-[var(--fg-1)]">
          登录服务暂时不可用，请重试。为避免误放行，管理内容不会展示。
        </p>
        <button
          type="button"
          onClick={onRetry}
          disabled={pending}
          className="mt-4 inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-[var(--radius-control)] border border-[var(--border-strong)] bg-[var(--bg-1)] px-4 text-sm transition-colors hover:bg-[var(--bg-2)] disabled:opacity-50"
        >
          {pending && <Loader2 className="h-4 w-4 animate-spin" />}
          {pending ? "重试中" : "重新验证"}
        </button>
      </div>
    </div>
  );
}

function AdminInner({ me }: { me: MaybeAdminUser | undefined }) {
  const [tab, setTab] = useState<Tab>("health");
  const activeTab = TABS.find((item) => item.key === tab) ?? TABS[0];
  const reduceMotion = useReducedMotion();

  return (
    <div className="flex h-[100dvh] min-h-0 w-full flex-col overflow-hidden bg-[var(--bg-0)] text-[var(--fg-0)]">
      <main className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden touch-pan-y scrollbar-thin">
        <div
          className={cn(
            "mx-auto max-w-7xl px-3 py-4 min-[380px]:px-4 md:px-8 md:py-8",
            adminMobileStyles.root,
          )}
        >
          <header className="mb-5 flex items-start justify-between gap-4 flex-wrap md:mb-7">
            <div className="min-w-0">
              <h1 className="type-page-title">管理后台</h1>
              <p className="type-body mt-1.5">
                按任务分组管理访问、运行状态、基础设施和系统配置。
              </p>
            </div>
            <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
              {me?.email && (
                <div className="flex min-h-11 items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)]/70 px-2.5 py-1.5 type-caption sm:min-h-8 sm:px-3">
                  <span className="h-1.5 w-1.5 rounded-full bg-accent shadow-[var(--shadow-amber)]" />
                  <span className="max-w-[140px] truncate text-[var(--fg-1)] sm:max-w-[180px]">
                    {me.email}
                  </span>
                  <span className="rounded-[var(--radius-control)] border border-accent-border bg-accent-soft px-1.5 py-0.5 text-[10px] font-medium text-accent">
                    管理员
                  </span>
                </div>
              )}
              <Link
                href="/"
                className="inline-flex min-h-11 items-center gap-1.5 px-2 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] sm:min-h-0 sm:px-0"
              >
                <ArrowLeft className="w-4 h-4" />
                返回工作台
              </Link>
            </div>
          </header>

          <TabNav tab={tab} onChange={setTab} />
          <PanelIntro tab={activeTab} />

          <div className="mt-5">
            <AnimatePresence mode="wait">
              <motion.div
                key={tab}
                initial={reduceMotion ? false : { opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={reduceMotion ? { duration: 0 } : { duration: 0.18, ease: "easeOut" }}
              >
                <AdminPanelContent tab={tab} onOpenTab={setTab} />
              </motion.div>
            </AnimatePresence>
          </div>
        </div>
      </main>
    </div>
  );
}

function AdminPanelContent({
  tab,
  onOpenTab,
}: {
  tab: Tab;
  onOpenTab: (tab: Tab) => void;
}) {
  const panels: Record<Tab, React.ReactNode> = {
    health: <HealthPanel onOpenTab={onOpenTab} />,
    emails: <AllowedEmailsPanel />,
    users: <UsersPanel />,
    events: <RequestEventsPanel />,
    invites: <InvitesPanel />,
    byok: <ByokPanel />,
    billing: <BillingPanel />,
    providers: <ProvidersPanel />,
    video_providers: <VideoProvidersPanel />,
    proxies: <ProxiesPanel />,
    telegram: <TelegramPanel />,
    settings: <SettingsPanel />,
    storage: <StoragePanel />,
    backups: <BackupsPanel />,
  };

  return panels[tab];
}

function TabNav({ tab, onChange }: { tab: Tab; onChange: (t: Tab) => void }) {
  const activeTab = TABS.find((item) => item.key === tab) ?? TABS[0];
  const ActiveTabIcon = activeTab.icon;

  return (
    <nav
      aria-label="管理后台菜单"
      data-testid="admin-tab-menu"
      className="space-y-3"
    >
      <div className="sticky top-0 z-20 -mx-3 border-y border-[var(--border-subtle)] bg-[var(--bg-0)]/95 px-3 py-3 backdrop-blur-xl min-[380px]:-mx-4 min-[380px]:px-4 md:hidden">
        <label htmlFor="admin-mobile-navigation" className="sr-only">
          管理后台页面
        </label>
        <div className="relative">
          <ActiveTabIcon
            aria-hidden="true"
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-accent"
          />
          <select
            id="admin-mobile-navigation"
            value={tab}
            onChange={(event) => onChange(event.target.value as Tab)}
            className="h-11 w-full appearance-none rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] pl-10 pr-10 text-base font-medium text-[var(--fg-0)] shadow-[var(--shadow-1)] outline-none focus:border-accent-border focus:ring-2 focus:ring-accent/20"
          >
            {TAB_GROUPS.map((group) => (
              <optgroup key={group.key} label={group.label}>
                {TABS.filter((item) => item.group === group.key).map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          <SlidersHorizontal
            aria-hidden="true"
            className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--fg-2)]"
          />
        </div>
      </div>

      <div className="hidden gap-6 md:grid md:grid-cols-2 lg:grid-cols-4">
        {TAB_GROUPS.map((group) => {
          const items = TABS.filter((item) => item.group === group.key);
          return (
            <section
              key={group.key}
              className="min-w-0 border-l border-[var(--border-subtle)] pl-3 first:border-l-0 first:pl-0"
            >
              <div className="pb-2">
                <p className="type-overline text-[var(--fg-1)]">
                  {group.label}
                </p>
                <p className="mt-0.5 type-caption text-[var(--fg-2)]">
                  {group.description}
                </p>
              </div>
              <div className="grid gap-0.5">
                {items.map((item) => {
                  const active = tab === item.key;
                  const Icon = item.icon;
                  return (
                    <button
                      key={item.key}
                      type="button"
                      aria-current={active ? "page" : undefined}
                      onClick={() => onChange(item.key)}
                      className={cn(
                        "flex min-h-11 w-full cursor-pointer items-center gap-2 rounded-[var(--radius-control)] px-2.5 py-2 text-left type-caption transition-colors motion-reduce:transition-none",
                        active
                          ? "bg-[var(--surface-selected)] text-[var(--fg-0)]"
                          : "text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
                      )}
                    >
                      <Icon
                        className={cn(
                          "h-3.5 w-3.5 shrink-0",
                          active
                            ? "text-[var(--accent-on)]"
                            : "text-[var(--fg-2)]",
                        )}
                      />
                      <span className="min-w-0 truncate">{item.label}</span>
                    </button>
                  );
                })}
              </div>
            </section>
          );
        })}
      </div>
    </nav>
  );
}

function PanelIntro({ tab }: { tab: TabMeta }) {
  const Icon = tab.icon;
  return (
    <div className="mt-5 flex flex-col gap-2 border-b border-[var(--border-subtle)] pb-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex min-w-0 items-start gap-3">
        <Icon className="mt-1 h-4 w-4 shrink-0 text-accent" />
        <div className="min-w-0">
          <h2 className="type-section-title">{tab.title}</h2>
          <p className="mt-1 max-w-3xl type-body-sm text-[var(--fg-2)]">
            {tab.description}
          </p>
        </div>
      </div>
    </div>
  );
}

// ———————————————————— 用户 ————————————————————

function UsersPanel() {
  const PAGE_SIZE = 50;
  const q = useAdminUsersInfiniteQuery({ limit: PAGE_SIZE });

  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState<UserRoleFilter>("all");
  const [historyUser, setHistoryUser] = useState<AdminUserOut | null>(null);
  const [passwordUser, setPasswordUser] = useState<AdminUserOut | null>(null);
  const [deleteUser, setDeleteUser] = useState<AdminUserOut | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const passwordGuardRef = useRef(false);
  const deleteGuardRef = useRef(false);
  const passwordMut = useSetAdminUserPasswordMutation({
    onSuccess: () => setPasswordUser(null),
    onSettled: () => {
      passwordGuardRef.current = false;
    },
  });
  const deleteMut = useDeleteAdminUserMutation({
    onSuccess: () => {
      setDeleteUser(null);
      setDeleteError(null);
    },
    onError: (err) => setDeleteError(err.message || "删除失败"),
    onSettled: () => {
      deleteGuardRef.current = false;
    },
  });

  const rows = useMemo(
    () => q.data?.pages.flatMap((p) => p.items) ?? [],
    [q.data],
  );

  const filtered = useMemo(() => {
    const normalizedSearch = search.trim().toLowerCase();
    return rows.filter((user) =>
      userMatchesFilters(user, roleFilter, normalizedSearch),
    );
  }, [rows, search, roleFilter]);

  return (
    <section className="space-y-5">
      {/* —— 过滤行 —— */}
      <div className="flex flex-col gap-3 md:flex-row md:items-center">
        <div
          className={`min-h-11 w-full flex-1 md:min-h-10 md:min-w-[220px] ${adminInputShellClassName}`}
        >
          <Search className="w-3.5 h-3.5 text-[var(--fg-2)]" />
          <label htmlFor="search-users" className="sr-only">
            搜索用户
          </label>
          <input
            id="search-users"
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索邮箱或名称"
            className="flex-1 bg-transparent text-base focus:outline-none placeholder:text-[var(--fg-2)] md:text-sm"
          />
        </div>
        <div
          role="tablist"
          aria-label="按角色过滤"
          className="inline-flex min-h-11 items-center gap-0.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] p-0.5 text-xs"
        >
          {(["all", "admin", "member"] as const).map((r) => (
            <button
              key={r}
              role="tab"
              aria-selected={roleFilter === r}
              type="button"
              onClick={() => setRoleFilter(r)}
              className={userRoleFilterClassName(roleFilter === r)}
            >
              {userRoleFilterLabel(r)}
            </button>
          ))}
        </div>
      </div>

      {/* —— 表格 —— */}
      <div className={tableShellClassName}>
        {q.isLoading && rows.length === 0 ? (
          <ListSkeleton rows={6} />
        ) : q.isError && rows.length === 0 ? (
          <ErrorBlock
            message={q.error?.message ?? "未知错误"}
            onRetry={() => void q.refetch()}
          />
        ) : filtered.length === 0 ? (
          <EmptyBlock
            title={emptyUsersTitle(rows.length)}
            description={emptyUsersDescription(rows.length)}
          />
        ) : (
          <>
            {/* 桌面端表格 */}
            <div className="hidden md:block overflow-x-auto [-webkit-overflow-scrolling:touch]">
              <table className="w-full text-sm">
                <thead className="text-xs uppercase tracking-wider text-[var(--fg-1)] border-b border-[var(--border)]">
                  <tr>
                    <th className="text-left py-3 px-4 font-medium">邮箱</th>
                    <th className="text-left py-3 px-4 font-medium">角色</th>
                    <th className="text-left py-3 px-4 font-medium">名称</th>
                    <th className="text-left py-3 px-4 font-medium">注册</th>
                    <th className="text-right py-3 px-4 font-medium">生成</th>
                    <th className="text-right py-3 px-4 font-medium">对话</th>
                    <th className="text-right py-3 px-4 font-medium">消息</th>
                    <th className="text-right py-3 px-4 font-medium">操作</th>
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
                      className="border-t border-[var(--border-subtle)] transition-colors hover:bg-[var(--bg-2)]/60"
                    >
                      <td className="py-3 px-4 text-[var(--fg-0)] break-all">
                        {u.email}
                      </td>
                      <td className="py-3 px-4">
                        <RoleBadge role={u.role} />
                      </td>
                      <td className="py-3 px-4 text-[var(--fg-1)] break-all">
                        {u.display_name ?? "—"}
                      </td>
                      <td className="py-3 px-4 text-[var(--fg-1)] font-mono text-xs tabular-nums whitespace-nowrap">
                        {formatISODate(u.created_at)}
                      </td>
                      <td className="py-3 px-4 text-right text-[var(--fg-0)] font-mono tabular-nums">
                        {u.generations_count}
                      </td>
                      <td className="py-3 px-4 text-right text-[var(--fg-0)] font-mono tabular-nums">
                        {u.completions_count}
                      </td>
                      <td className="py-3 px-4 text-right text-[var(--fg-0)] font-mono tabular-nums">
                        {u.messages_count}
                      </td>
                      <td className="py-3 px-4">
                        <UserActions
                          onHistory={() => setHistoryUser(u)}
                          onPassword={() => {
                            passwordMut.reset();
                            setPasswordUser(u);
                          }}
                          onDelete={() => {
                            deleteMut.reset();
                            setDeleteError(null);
                            setDeleteUser(u);
                          }}
                        />
                      </td>
                    </motion.tr>
                  ))}
                </tbody>
              </table>
            </div>
            {/* 移动端卡片列表 */}
            <ul className="divide-y divide-[var(--border-subtle)] md:hidden">
              {filtered.map((u) => (
                <li key={u.id} className="p-4 space-y-2">
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm text-[var(--fg-0)] break-all min-w-0 flex-1">
                      {u.email}
                    </span>
                    <div className="shrink-0">
                      <RoleBadge role={u.role} />
                    </div>
                  </div>
                  {u.display_name && (
                    <div className="text-xs text-[var(--fg-1)] break-all">
                      {u.display_name}
                    </div>
                  )}
                  <div className="text-sm text-[var(--fg-2)] font-mono tabular-nums">
                    {formatISODate(u.created_at)}
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-xs">
                    <MiniStat label="生成" value={u.generations_count} />
                    <MiniStat label="对话" value={u.completions_count} />
                    <MiniStat label="消息" value={u.messages_count} />
                  </div>
                  <UserActions
                    onHistory={() => setHistoryUser(u)}
                    onPassword={() => {
                      passwordMut.reset();
                      setPasswordUser(u);
                    }}
                    onDelete={() => {
                      deleteMut.reset();
                      setDeleteError(null);
                      setDeleteUser(u);
                    }}
                    mobile
                  />
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
            className="inline-flex min-h-11 items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-5 text-sm transition-colors hover:bg-[var(--bg-3)] disabled:opacity-50 md:min-h-9"
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

      {historyUser && (
        <UserHistoryDialog
          user={historyUser}
          onClose={() => setHistoryUser(null)}
        />
      )}
      {passwordUser && (
        <PasswordDialog
          user={passwordUser}
          pending={passwordMut.isPending}
          error={passwordMut.error?.message ?? null}
          onClose={() => {
            if (passwordMut.isPending) return;
            passwordMut.reset();
            setPasswordUser(null);
          }}
          onSubmit={(password) => {
            if (passwordGuardRef.current) return;
            passwordGuardRef.current = true;
            passwordMut.mutate({ userId: passwordUser.id, password });
          }}
        />
      )}
      <ConfirmDialog
        open={deleteUser != null}
        onOpenChange={(open) => {
          if (!open && !deleteMut.isPending) {
            deleteMut.reset();
            setDeleteError(null);
            setDeleteUser(null);
          }
        }}
        title="删除用户"
        description={
          <DeleteUserDescription user={deleteUser} error={deleteError} />
        }
        confirmText="删除"
        cancelText="取消"
        tone="danger"
        confirming={deleteMut.isPending}
        onConfirm={() => {
          if (!deleteUser || deleteGuardRef.current) return;
          deleteGuardRef.current = true;
          setDeleteError(null);
          deleteMut.mutate(deleteUser.id);
        }}
      />
    </section>
  );
}

function DeleteUserDescription({
  user,
  error,
}: {
  user: AdminUserOut | null;
  error: string | null;
}) {
  if (!user) return null;
  return (
    <span className="block">
      <span>
        将软删除 <span className="font-mono">{user.email}</span>
        ，并撤销会话、隐藏会话和图片。
      </span>
      {error && (
        <span
          role="alert"
          aria-live="assertive"
          className="mt-2 block text-danger"
        >
          {error}
        </span>
      )}
    </span>
  );
}

function userMatchesFilters(
  user: AdminUserOut,
  roleFilter: UserRoleFilter,
  normalizedSearch: string,
): boolean {
  const matchesRole = roleFilter === "all" || user.role === roleFilter;
  const matchesSearch =
    normalizedSearch.length === 0 ||
    user.email.toLowerCase().includes(normalizedSearch) ||
    (user.display_name ?? "").toLowerCase().includes(normalizedSearch);
  return matchesRole && matchesSearch;
}

function userRoleFilterLabel(role: UserRoleFilter): string {
  const labels: Record<UserRoleFilter, string> = {
    all: "全部",
    admin: "管理员",
    member: "成员",
  };
  return labels[role];
}

function userRoleFilterClassName(active: boolean): string {
  return cn(
    "min-h-11 rounded-[var(--radius-control)] px-3 transition-colors md:min-h-8",
    active
      ? "bg-[var(--bg-3)] text-[var(--fg-0)]"
      : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
  );
}

function emptyUsersDescription(rowCount: number): string {
  return rowCount === 0 ? "注册的用户会出现在这里" : "试试切换角色或换个关键词";
}

function emptyUsersTitle(rowCount: number): string {
  return rowCount === 0 ? "暂无用户" : "没有匹配结果";
}

function UserActions({
  onHistory,
  onPassword,
  onDelete,
  mobile = false,
}: {
  onHistory: () => void;
  onPassword: () => void;
  onDelete: () => void;
  mobile?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex items-center gap-1.5",
        mobile ? "pt-1" : "justify-end",
      )}
    >
      <ActionIcon label="历史" icon={Eye} onClick={onHistory} />
      <ActionIcon label="改密码" icon={KeyRound} onClick={onPassword} />
      <ActionIcon label="删除" icon={Trash2} onClick={onDelete} danger />
    </div>
  );
}

function ActionIcon({
  label,
  icon: Icon,
  onClick,
  danger = false,
}: {
  label: string;
  icon: LucideIcon;
  onClick: () => void;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      className={cn(
        "inline-flex h-11 w-11 items-center justify-center rounded-[var(--radius-control)] border transition-colors md:h-8 md:w-8",
        danger
          ? "border-danger-border bg-danger-soft text-[var(--danger-fg)] hover:brightness-110"
          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)] hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]",
      )}
    >
      <Icon className="h-3.5 w-3.5" />
    </button>
  );
}

function UserHistoryDialog({
  user,
  onClose,
}: {
  user: AdminUserOut;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  useBodyScrollLock(true);
  const onDialogKeyDown = useModalLayer({
    open: true,
    rootRef: dialogRef,
    onClose,
  });
  const q = useAdminUserHistoryQuery(user.id);
  const items = q.data?.items ?? [];

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/55 p-0 backdrop-blur-sm sm:items-center sm:p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <motion.div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`生成历史：${user.email}`}
        tabIndex={-1}
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        onKeyDown={onDialogKeyDown}
        className="surface-dialog mobile-dialog-panel flex max-h-[86vh] w-full max-w-4xl flex-col overflow-hidden sm:rounded-[var(--radius-dialog)]"
      >
        <div className="flex items-start justify-between gap-4 border-b border-[var(--border)] p-4">
          <div className="min-w-0">
            <h2 className="type-card-title">生成历史</h2>
            <p className="mt-1 break-all text-xs text-[var(--fg-2)]">
              {user.email}
            </p>
          </div>
          <button
            type="button"
            aria-label="关闭"
            onClick={onClose}
            className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] sm:h-8 sm:w-8"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {q.isLoading ? (
            <ListSkeleton rows={5} />
          ) : q.isError ? (
            <ErrorBlock
              message={q.error?.message ?? "未知错误"}
              onRetry={() => void q.refetch()}
            />
          ) : items.length === 0 ? (
            <EmptyBlock title="暂无生成历史" />
          ) : (
            <div className="space-y-3">
              {items.map((item) => (
                <div
                  key={item.id}
                  className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] p-3"
                >
                  <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                    <div className="min-w-0 space-y-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <StatusPill status={item.status} />
                        <RetentionPill state={item.retention_state} />
                        <span className="font-mono text-xs text-[var(--fg-2)]">
                          {formatISODate(item.created_at)}
                        </span>
                      </div>
                      <p className="line-clamp-3 text-sm text-[var(--fg-0)]">
                        {item.prompt || "无提示词"}
                      </p>
                      {item.conversation_title && (
                        <p className="text-xs text-[var(--fg-2)]">
                          {item.conversation_title}
                        </p>
                      )}
                    </div>
                    <div className="grid grid-cols-3 gap-2 md:w-44">
                      {item.images.slice(0, 3).map((image) => (
                        <a
                          key={image.id}
                          href={image.url}
                          target="_blank"
                          rel="noreferrer"
                          className="relative aspect-square overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)]"
                        >
                          <NextImage
                            src={
                              image.thumb_url ?? image.preview_url ?? image.url
                            }
                            alt=""
                            fill
                            sizes="64px"
                            className="object-cover"
                            unoptimized
                          />
                        </a>
                      ))}
                      {item.images.length === 0 && (
                        <div className="col-span-3 flex aspect-[3/1] items-center justify-center rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)] text-[var(--fg-2)]">
                          <Images className="h-4 w-4" />
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </motion.div>
    </motion.div>
  );
}

function PasswordDialog({
  user,
  pending,
  error,
  onClose,
  onSubmit,
}: {
  user: AdminUserOut;
  pending: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (password: string) => void;
}) {
  useBodyScrollLock(true);
  const [password, setPassword] = useState("");
  const canSubmit = password.length >= 8 && !pending;

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/55 p-0 backdrop-blur-sm sm:items-center sm:p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !pending) onClose();
      }}
    >
      <motion.form
        role="dialog"
        aria-modal="true"
        aria-label={`修改密码：${user.email}`}
        initial={{ opacity: 0, y: 16, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        onSubmit={(e) => {
          e.preventDefault();
          if (canSubmit) onSubmit(password);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape" && !pending) onClose();
        }}
        className="surface-dialog mobile-dialog-panel w-full max-w-sm space-y-4 overflow-hidden p-5 sm:rounded-[var(--radius-dialog)]"
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="type-card-title">修改密码</h2>
            <p className="mt-1 break-all text-xs text-[var(--fg-2)]">
              {user.email}
            </p>
          </div>
          <button
            type="button"
            aria-label="关闭"
            onClick={onClose}
            disabled={pending}
            className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] disabled:opacity-50 sm:h-8 sm:w-8"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <label className="block space-y-1.5">
          <span className="text-xs text-[var(--fg-2)]">新密码</span>
          <input
            name="new-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={8}
            maxLength={128}
            autoFocus
            autoComplete="new-password"
            className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-base text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--border-strong)] sm:h-10 sm:text-sm"
          />
        </label>
        {error && (
          <p role="alert" className="text-xs text-[var(--danger)]">
            {error}
          </p>
        )}
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onClose}
            disabled={pending}
            className="h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-sm text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] disabled:opacity-50 sm:h-9 sm:text-xs"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={!canSubmit}
            className="inline-flex h-11 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border-strong)] bg-[var(--fg-0)] px-3 text-sm text-[var(--bg-0)] transition-colors disabled:opacity-50 sm:h-9 sm:text-xs"
          >
            {pending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            保存
          </button>
        </div>
      </motion.form>
    </motion.div>
  );
}

function StatusPill({ status }: { status: string }) {
  return (
    <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 text-xs text-[var(--fg-1)]">
      {status}
    </span>
  );
}

function RetentionPill({ state }: { state: "active" | "hidden" | "deleted" }) {
  const label =
    state === "hidden" ? "已隐藏" : state === "deleted" ? "已删除" : "可见";
  return (
    <span className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 text-xs text-[var(--fg-2)]">
      {label}
    </span>
  );
}

function MiniStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-2)]/70 px-2 py-1.5">
      <div className="text-[11px] uppercase tracking-wider text-[var(--fg-2)]">
        {label}
      </div>
      <div className="text-base text-[var(--fg-0)] font-mono tabular-nums">
        {value}
      </div>
    </div>
  );
}

function RoleBadge({ role }: { role: "admin" | "member" }) {
  if (role === "admin") {
    return (
      <span className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-accent-border bg-accent-soft px-2 py-0.5 text-xs text-accent">
        <UserCog className="w-3 h-3" />
        管理员
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-0.5 text-xs text-[var(--fg-1)]">
      <UsersIcon className="w-3 h-3" />
      成员
    </span>
  );
}
