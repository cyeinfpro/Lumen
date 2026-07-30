"use client";

import { useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import {
  Eye,
  KeyRound,
  Loader2,
  Search,
  Trash2,
  UserCog,
  Users as UsersIcon,
  type LucideIcon,
} from "lucide-react";

import {
  useAdminUsersInfiniteQuery,
  useDeleteAdminUserMutation,
  useSetAdminUserPasswordMutation,
} from "@/lib/queries";
import type { AdminUserOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import {
  EmptyBlock,
  ErrorBlock,
  ListSkeleton,
} from "../_components/AdminFeedback";
import {
  adminInputShellClassName,
  formatISODate,
  tableShellClassName,
} from "../_components/adminUi";
import {
  DeleteUserDescription,
  PasswordDialog,
  UserHistoryDialog,
} from "./users/UserDialogs";
import {
  emptyUsersDescription,
  emptyUsersTitle,
  userMatchesFilters,
  userRoleFilterLabel,
  type UserRoleFilter,
} from "./users/model";

const PAGE_SIZE = 50;

export function UsersPanel() {
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
          {(["all", "admin", "member"] as const).map((role) => (
            <button
              key={role}
              role="tab"
              aria-selected={roleFilter === role}
              type="button"
              onClick={() => setRoleFilter(role)}
              className={userRoleFilterClassName(roleFilter === role)}
            >
              {userRoleFilterLabel(role)}
            </button>
          ))}
        </div>
      </div>

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
                  {filtered.map((user, index) => (
                    <motion.tr
                      key={user.id}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{
                        duration: 0.18,
                        delay: Math.min(index * 0.02, 0.2),
                      }}
                      className="border-t border-[var(--border-subtle)] transition-colors hover:bg-[var(--bg-2)]/60"
                    >
                      <td className="py-3 px-4 text-[var(--fg-0)] break-all">
                        {user.email}
                      </td>
                      <td className="py-3 px-4">
                        <RoleBadge role={user.role} />
                      </td>
                      <td className="py-3 px-4 text-[var(--fg-1)] break-all">
                        {user.display_name ?? "—"}
                      </td>
                      <td className="py-3 px-4 text-[var(--fg-1)] font-mono text-xs tabular-nums whitespace-nowrap">
                        {formatISODate(user.created_at)}
                      </td>
                      <td className="py-3 px-4 text-right text-[var(--fg-0)] font-mono tabular-nums">
                        {user.generations_count}
                      </td>
                      <td className="py-3 px-4 text-right text-[var(--fg-0)] font-mono tabular-nums">
                        {user.completions_count}
                      </td>
                      <td className="py-3 px-4 text-right text-[var(--fg-0)] font-mono tabular-nums">
                        {user.messages_count}
                      </td>
                      <td className="py-3 px-4">
                        <UserActions
                          onHistory={() => setHistoryUser(user)}
                          onPassword={() => {
                            passwordMut.reset();
                            setPasswordUser(user);
                          }}
                          onDelete={() => {
                            deleteMut.reset();
                            setDeleteError(null);
                            setDeleteUser(user);
                          }}
                        />
                      </td>
                    </motion.tr>
                  ))}
                </tbody>
              </table>
            </div>
            <ul className="divide-y divide-[var(--border-subtle)] md:hidden">
              {filtered.map((user) => (
                <li key={user.id} className="p-4 space-y-2">
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm text-[var(--fg-0)] break-all min-w-0 flex-1">
                      {user.email}
                    </span>
                    <div className="shrink-0">
                      <RoleBadge role={user.role} />
                    </div>
                  </div>
                  {user.display_name && (
                    <div className="text-xs text-[var(--fg-1)] break-all">
                      {user.display_name}
                    </div>
                  )}
                  <div className="text-sm text-[var(--fg-2)] font-mono tabular-nums">
                    {formatISODate(user.created_at)}
                  </div>
                  <div className="grid grid-cols-3 gap-2 text-xs">
                    <MiniStat label="生成" value={user.generations_count} />
                    <MiniStat label="对话" value={user.completions_count} />
                    <MiniStat label="消息" value={user.messages_count} />
                  </div>
                  <UserActions
                    onHistory={() => setHistoryUser(user)}
                    onPassword={() => {
                      passwordMut.reset();
                      setPasswordUser(user);
                    }}
                    onDelete={() => {
                      deleteMut.reset();
                      setDeleteError(null);
                      setDeleteUser(user);
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

function userRoleFilterClassName(active: boolean): string {
  return cn(
    "min-h-11 rounded-[var(--radius-control)] px-3 transition-colors md:min-h-8",
    active
      ? "bg-[var(--bg-3)] text-[var(--fg-0)]"
      : "text-[var(--fg-1)] hover:text-[var(--fg-0)]",
  );
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
