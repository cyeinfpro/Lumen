"use client";

import { useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import { AlertCircle, Loader2, Search } from "lucide-react";

import {
  useAddAllowedEmailMutation,
  useAllowedEmailsQuery,
  useRemoveAllowedEmailMutation,
} from "@/lib/queries";
import { ApiError } from "@/lib/apiClient";
import { EmptyBlock, ErrorBlock, ListSkeleton } from "./AdminFeedback";
import {
  adminInputShellClassName,
  formatISODate,
  tableShellClassName,
} from "./adminUi";

export function AllowedEmailsPanel() {
  const query = useAllowedEmailsQuery();
  const [email, setEmail] = useState("");
  const [search, setSearch] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [pendingRemoveId, setPendingRemoveId] = useState<string | null>(null);
  const addGuardRef = useRef(false);
  const removeGuardRef = useRef(false);

  const addMutation = useAddAllowedEmailMutation({
    onSuccess: () => {
      setEmail("");
      setFormError(null);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setFormError("该邮箱已在白名单中");
      } else {
        setFormError(error.message || "添加失败");
      }
    },
  });

  const removeMutation = useRemoveAllowedEmailMutation({
    onError: (error) => setRemoveError(error.message || "移除失败"),
    onSettled: () => {
      removeGuardRef.current = false;
      setPendingRemoveId(null);
    },
  });

  const onSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (addGuardRef.current) return;
    setFormError(null);
    const trimmed = email.trim();
    if (!trimmed) {
      setFormError("邮箱未填");
      return;
    }
    addGuardRef.current = true;
    addMutation.mutate(trimmed, {
      onSettled: () => {
        addGuardRef.current = false;
      },
    });
  };

  const removeAllowedEmail = (id: string) => {
    if (removeGuardRef.current) return;
    removeGuardRef.current = true;
    setRemoveError(null);
    removeMutation.mutate(id);
  };

  const filtered = useMemo(() => {
    const rows = query.data?.items ?? [];
    const normalizedSearch = search.trim().toLowerCase();
    if (!normalizedSearch) return rows;
    return rows.filter(
      (row) =>
        row.email.toLowerCase().includes(normalizedSearch) ||
        (row.invited_by_email ?? "").toLowerCase().includes(normalizedSearch),
    );
  }, [query.data, search]);

  return (
    <section className="space-y-5">
      <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)] backdrop-blur-sm md:p-5">
        <form
          onSubmit={onSubmit}
          className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center"
        >
          <div className={`min-h-11 flex-1 ${adminInputShellClassName}`}>
            <label htmlFor="add-allowed-email" className="sr-only">
              邮箱
            </label>
            <input
              id="add-allowed-email"
              type="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              placeholder="name@示例.com"
              autoComplete="off"
              className="flex-1 bg-transparent text-sm placeholder:text-[var(--fg-2)] focus:outline-none"
            />
          </div>
          <button
            type="submit"
            disabled={addMutation.isPending}
            className="inline-flex min-h-11 items-center justify-center gap-1.5 rounded-[var(--radius-control)] bg-accent px-4 text-sm font-medium text-[var(--accent-on)] transition-[filter,transform] hover:brightness-110 active:scale-[0.97] disabled:opacity-50"
          >
            {addMutation.isPending ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> 添加中
              </>
            ) : (
              "添加白名单"
            )}
          </button>
        </form>
        {formError && (
          <p
            role="alert"
            aria-live="assertive"
            className="flex items-center gap-1.5 type-caption text-danger"
          >
            <AlertCircle className="h-3.5 w-3.5" />
            {formError}
          </p>
        )}
        {removeError && (
          <p
            role="alert"
            aria-live="assertive"
            className="flex items-center gap-1.5 type-caption text-danger"
          >
            <AlertCircle className="h-3.5 w-3.5" />
            {removeError}
          </p>
        )}

        <div className={`mt-3 min-h-11 ${adminInputShellClassName}`}>
          <Search className="h-3.5 w-3.5 text-[var(--fg-2)]" />
          <label htmlFor="search-allowed" className="sr-only">
            搜索白名单
          </label>
          <input
            id="search-allowed"
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索邮箱或邀请人"
            className="flex-1 bg-transparent text-xs placeholder:text-[var(--fg-2)] focus:outline-none"
          />
        </div>
      </div>

      <div className={tableShellClassName}>
        {query.isLoading ? (
          <ListSkeleton rows={4} />
        ) : query.isError ? (
          <ErrorBlock
            message={query.error?.message ?? "未知错误"}
            onRetry={() => void query.refetch()}
          />
        ) : filtered.length === 0 ? (
          <EmptyBlock
            title={search ? "没有匹配结果" : "白名单为空"}
            description={
              search ? "试试换个关键词" : "添加邮箱允许该用户注册 Lumen"
            }
          />
        ) : (
          <>
            <div className="hidden overflow-x-auto [-webkit-overflow-scrolling:touch] md:block">
              <table className="w-full text-sm">
                <thead className="border-b border-[var(--border)] text-xs uppercase tracking-wider text-[var(--fg-1)]">
                  <tr>
                    <th className="px-4 py-3 text-left font-medium">邮箱</th>
                    <th className="px-4 py-3 text-left font-medium">邀请人</th>
                    <th className="px-4 py-3 text-left font-medium">创建时间</th>
                    <th className="px-4 py-3 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((row, index) => (
                    <motion.tr
                      key={row.id}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{
                        duration: 0.18,
                        delay: Math.min(index * 0.03, 0.18),
                      }}
                      className="border-t border-[var(--border-subtle)] align-middle transition-colors hover:bg-[var(--bg-2)]/60"
                    >
                      <td className="break-all px-4 py-3 text-[var(--fg-0)]">
                        {row.email}
                      </td>
                      <td className="break-all px-4 py-3 text-[var(--fg-1)]">
                        {row.invited_by_email ?? "—"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 font-mono text-xs tabular-nums text-[var(--fg-1)]">
                        {formatISODate(row.created_at)}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <ConfirmInlineRemove
                          disabled={removeMutation.isPending}
                          pending={
                            removeMutation.isPending &&
                            pendingRemoveId === row.id
                          }
                          active={pendingRemoveId === row.id}
                          onActivate={() => setPendingRemoveId(row.id)}
                          onCancel={() => setPendingRemoveId(null)}
                          onConfirm={() => removeAllowedEmail(row.id)}
                          email={row.email}
                        />
                      </td>
                    </motion.tr>
                  ))}
                </tbody>
              </table>
            </div>
            <ul className="divide-y divide-[var(--border-subtle)] md:hidden">
              {filtered.map((row) => (
                <li key={row.id} className="space-y-2 p-4">
                  <div className="flex items-start justify-between gap-2">
                    <span className="min-w-0 break-all text-sm text-[var(--fg-0)]">
                      {row.email}
                    </span>
                    <ConfirmInlineRemove
                      disabled={removeMutation.isPending}
                      pending={
                        removeMutation.isPending && pendingRemoveId === row.id
                      }
                      active={pendingRemoveId === row.id}
                      onActivate={() => setPendingRemoveId(row.id)}
                      onCancel={() => setPendingRemoveId(null)}
                      onConfirm={() => removeAllowedEmail(row.id)}
                      email={row.email}
                    />
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-[var(--fg-2)]">
                        邀请人
                      </div>
                      <div className="break-all text-[var(--fg-1)]">
                        {row.invited_by_email ?? "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] uppercase tracking-wider text-[var(--fg-2)]">
                        创建
                      </div>
                      <div className="font-mono tabular-nums text-[var(--fg-1)]">
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
  disabled,
  pending,
  active,
  onActivate,
  onCancel,
  onConfirm,
  email,
}: {
  disabled: boolean;
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
        disabled={disabled}
        className="inline-flex min-h-11 shrink-0 items-center justify-center px-2.5 type-caption text-danger transition-colors hover:opacity-90 md:min-h-8"
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
      className="inline-flex shrink-0 items-center gap-2"
    >
      <span className="hidden text-xs text-[var(--fg-1)] sm:inline">确认?</span>
      <button
        type="button"
        onClick={onConfirm}
        disabled={disabled}
        className="min-h-11 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-3 py-1.5 type-caption text-[var(--danger-fg)] transition-colors hover:brightness-110 disabled:opacity-50 md:min-h-8"
      >
        {pending ? "移除中" : "移除"}
      </button>
      <button
        type="button"
        onClick={onCancel}
        disabled={disabled}
        className="min-h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 py-1.5 text-xs text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] disabled:opacity-50 md:min-h-8"
      >
        取消
      </button>
    </motion.div>
  );
}
