"use client";

// Lumen V1.0 管理面板：邀请链接。
// - 顶部生成表单：邮箱(可空) / 过期天数 / 角色 / 提交
// - 生成成功 → 高亮卡片展示，一键复制 URL
// - 列表：URL / Email / Role / 状态 / 过期 / 创建 / 撤销（内嵌确认）
// - 三态 loading/empty/error；ApiError 分支细化文案

import { useMemo, useState, useSyncExternalStore } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { format } from "date-fns";
import {
  AlertCircle,
  Check,
  Copy,
  Link as LinkIcon,
  Loader2,
  UserCog,
  Users as UsersIcon,
  X,
} from "lucide-react";

import {
  useCreateInviteLinkMutation,
  useInviteLinksQuery,
  useRevokeInviteLinkMutation,
} from "@/lib/queries";
import { ApiError } from "@/lib/apiClient";
import type { InviteLinkOut } from "@/lib/types";
import { EmptyBlock, ErrorBlock, ListSkeleton } from "../page";

type InviteStatus = "valid" | "used" | "revoked" | "expired";

function statusOf(row: InviteLinkOut, now: number = Date.now()): InviteStatus {
  if (row.revoked_at) return "revoked";
  if (row.used_at) return "used";
  if (row.expires_at) {
    const exp = new Date(row.expires_at).getTime();
    if (Number.isFinite(exp) && exp < now) return "expired";
  }
  return "valid";
}

export function InvitesPanel() {
  const q = useInviteLinksQuery();

  const [email, setEmail] = useState("");
  const [days, setDays] = useState<number>(7);
  const [role, setRole] = useState<"member" | "admin">("member");
  const [formError, setFormError] = useState<string | null>(null);
  const [created, setCreated] = useState<InviteLinkOut | null>(null);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [pendingRevokeId, setPendingRevokeId] = useState<string | null>(null);

  const createMut = useCreateInviteLinkMutation({
    onSuccess: (data) => {
      setCreated(data);
      setFormError(null);
      setEmail("");
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        if (err.status === 409) setFormError("已存在相同邮箱的有效邀请");
        else if (err.status === 422) setFormError(err.message || "参数无效");
        else setFormError(err.message || "创建失败");
      } else {
        setFormError(err.message || "创建失败");
      }
    },
  });

  const revokeMut = useRevokeInviteLinkMutation({
    onSettled: () => setPendingRevokeId(null),
  });

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    const trimmed = email.trim();
    const safeDays = Math.max(1, Math.min(365, Math.floor(days || 0)));
    if (!Number.isFinite(safeDays) || safeDays < 1) {
      setFormError("有效期天数必须是 1–365 之间的整数");
      return;
    }
    createMut.mutate({
      email: trimmed === "" ? null : trimmed,
      expires_in_days: safeDays,
      role,
    });
  };

  const onCopy = async (key: string, text: string) => {
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard) {
        await navigator.clipboard.writeText(text);
      } else if (typeof document !== "undefined") {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "absolute";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        try {
          document.execCommand("copy");
        } finally {
          document.body.removeChild(ta);
        }
      }
      setCopiedKey(key);
      setTimeout(() => {
        setCopiedKey((prev) => (prev === key ? null : prev));
      }, 1500);
    } catch {
      setCopiedKey(null);
    }
  };

  const rows = useMemo(() => q.data?.items ?? [], [q.data]);
  const now = useSyncExternalStore(subscribeTime, getNow, getNowSSR);

  return (
    <section className="space-y-5">
      {/* —— 生成表单 —— */}
      <form
        onSubmit={onSubmit}
        className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-white/10 rounded-2xl p-5 space-y-4"
      >
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-1 sm:gap-2">
          <h2 className="text-sm font-medium text-neutral-100">生成邀请链接</h2>
          <p className="text-xs text-neutral-500">
            邮箱留空表示任何人均可使用
          </p>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-[1fr_auto_auto_auto] gap-3 items-stretch">
          <FormField id="invite-new-email" label="邀请邮箱（可选）">
            <input
              id="invite-new-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="someone@example.com"
              autoComplete="off"
              className="w-full min-h-[44px] sm:h-9 px-3 rounded-xl bg-[var(--bg-0)]/60 border border-white/10 text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 placeholder:text-neutral-600 transition-colors"
            />
          </FormField>
          <FormField id="invite-new-days" label="有效期（天）">
            <input
              id="invite-new-days"
              type="number"
              min={1}
              max={365}
              inputMode="numeric"
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              className="w-full sm:w-24 min-h-[44px] sm:h-9 px-3 rounded-xl bg-[var(--bg-0)]/60 border border-white/10 text-sm font-mono tabular-nums focus:outline-none focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 transition-colors"
            />
          </FormField>
          <FormField id="invite-new-role" label="角色">
            <select
              id="invite-new-role"
              value={role}
              onChange={(e) => setRole(e.target.value as "member" | "admin")}
              className="w-full min-h-[44px] sm:h-9 px-3 rounded-xl bg-[var(--bg-0)]/60 border border-white/10 text-sm focus:outline-none focus:border-[var(--color-lumen-amber)]/50 focus:ring-2 focus:ring-[var(--color-lumen-amber)]/25 transition-colors"
            >
              <option value="member">member</option>
              <option value="admin">admin</option>
            </select>
          </FormField>
          <div className="self-end">
            <button
              type="submit"
              disabled={createMut.isPending}
              className="inline-flex items-center justify-center gap-1.5 w-full min-h-[44px] sm:h-9 px-4 rounded-xl bg-[var(--color-lumen-amber)] hover:brightness-110 active:scale-[0.97] text-black text-sm font-medium disabled:opacity-50 transition-all"
            >
              {createMut.isPending ? (
                <>
                  <Loader2 className="w-3.5 h-3.5 animate-spin" /> 生成中
                </>
              ) : (
                <>
                  <LinkIcon className="w-3.5 h-3.5" /> 生成链接
                </>
              )}
            </button>
          </div>
        </div>
        {formError && (
          <p className="flex items-center gap-1.5 text-xs text-red-300">
            <AlertCircle className="w-3.5 h-3.5" /> {formError}
          </p>
        )}

        <AnimatePresence>
          {created && (
            <motion.div
              key={created.id}
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.2 }}
              className="rounded-xl border border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/[0.06] p-4 space-y-3"
            >
              <div className="flex items-center justify-between">
                <span className="inline-flex items-center gap-1.5 text-xs uppercase tracking-wider text-[var(--color-lumen-amber)]">
                  <Check className="w-3.5 h-3.5" /> 新邀请已生成
                </span>
                <button
                  type="button"
                  onClick={() => setCreated(null)}
                  className="text-neutral-400 hover:text-neutral-100 transition-colors"
                  aria-label="关闭"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
              <div className="flex items-stretch gap-2">
                <code className="flex-1 px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-xs font-mono text-neutral-200 break-all">
                  {created.url}
                </code>
                <button
                  type="button"
                  onClick={() => onCopy(`new:${created.id}`, created.url)}
                  className="inline-flex items-center gap-1.5 px-3 rounded-lg bg-white/10 hover:bg-white/15 border border-white/10 text-xs whitespace-nowrap transition-colors"
                >
                  {copiedKey === `new:${created.id}` ? (
                    <>
                      <Check className="w-3.5 h-3.5 text-emerald-300" /> 已复制
                    </>
                  ) : (
                    <>
                      <Copy className="w-3.5 h-3.5" /> 复制
                    </>
                  )}
                </button>
              </div>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
                <Field label="Token">
                  <span className="font-mono text-neutral-300">
                    {created.token.slice(0, 12)}…
                  </span>
                </Field>
                <Field label="Email">
                  <span className="text-neutral-300">
                    {created.email ?? "—"}
                  </span>
                </Field>
                <Field label="Role">
                  <RoleBadge role={created.role} />
                </Field>
                <Field label="过期时间">
                  <span className="text-neutral-300 font-mono tabular-nums">
                    {created.expires_at
                      ? formatISODate(created.expires_at)
                      : "永久"}
                  </span>
                </Field>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </form>

      {/* —— 列表 —— */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-white/10 rounded-2xl overflow-hidden">
        {q.isLoading ? (
          <ListSkeleton rows={5} />
        ) : q.isError ? (
          <ErrorBlock
            message={q.error?.message ?? "未知错误"}
            onRetry={() => void q.refetch()}
          />
        ) : rows.length === 0 ? (
          <EmptyBlock
            title="暂无邀请链接"
            description="生成一条邀请链接让新朋友加入 Lumen"
          />
        ) : (
          <>
          {/* 移动端卡片列表 */}
          <ul className="md:hidden space-y-2 p-2">
            {rows.map((row) => {
              const st = statusOf(row, now);
              const canRevoke = st === "valid";
              const isConfirming = pendingRevokeId === row.id;
              return (
                <li
                  key={row.id}
                  className="p-3 border border-white/10 rounded-lg space-y-3"
                >
                  <div className="flex flex-col gap-2">
                    <code className="w-full min-w-0 px-2 py-2 rounded-md bg-black/30 border border-white/10 text-xs font-mono text-neutral-200 break-all leading-relaxed">
                      {row.url}
                    </code>
                    <button
                      type="button"
                      onClick={() => onCopy(`row:${row.id}`, row.url)}
                      className="w-full inline-flex items-center justify-center gap-1.5 min-h-[40px] px-3 rounded-md text-sm text-neutral-200 hover:text-neutral-100 bg-white/5 hover:bg-white/10 border border-white/10 transition-colors"
                      aria-label="复制链接"
                    >
                      {copiedKey === `row:${row.id}` ? (
                        <>
                          <Check className="w-3.5 h-3.5 text-emerald-300" /> 已复制
                        </>
                      ) : (
                        <>
                          <Copy className="w-3.5 h-3.5" /> 复制链接
                        </>
                      )}
                    </button>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div>
                      <div className="text-[11px] uppercase tracking-wider text-neutral-500">
                        Email
                      </div>
                      <div className="text-neutral-300 break-all">
                        {row.email ?? "—"}
                      </div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase tracking-wider text-neutral-500">
                        Role
                      </div>
                      <div className="mt-0.5">
                        <RoleBadge role={row.role} />
                      </div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase tracking-wider text-neutral-500">
                        状态
                      </div>
                      <div className="mt-0.5">
                        <StatusBadge status={st} usedBy={row.used_by_email} />
                      </div>
                    </div>
                    <div>
                      <div className="text-[11px] uppercase tracking-wider text-neutral-500">
                        过期
                      </div>
                      <div className="text-sm text-neutral-300 font-mono tabular-nums break-all">
                        {row.expires_at ? formatISODate(row.expires_at) : "永久"}
                      </div>
                    </div>
                    <div className="col-span-2">
                      <div className="text-[11px] uppercase tracking-wider text-neutral-500">
                        创建
                      </div>
                      <div className="text-sm text-neutral-400 font-mono tabular-nums break-all">
                        {formatISODate(row.created_at)}
                      </div>
                    </div>
                  </div>
                  <div className="flex justify-end pt-1">
                    {!canRevoke ? (
                      <span className="text-xs text-neutral-600">—</span>
                    ) : isConfirming ? (
                      <div className="inline-flex items-center gap-2">
                        <span className="text-xs text-neutral-400 hidden sm:inline">
                          撤销?
                        </span>
                        <button
                          type="button"
                          onClick={() => revokeMut.mutate(row.id)}
                          disabled={revokeMut.isPending}
                          aria-disabled={revokeMut.isPending}
                          className="text-xs px-3 min-h-[36px] rounded-md bg-red-500/80 hover:bg-red-500 text-white disabled:opacity-50 transition-colors"
                        >
                          {revokeMut.isPending ? "撤销中" : "确认撤销"}
                        </button>
                        <button
                          type="button"
                          onClick={() => setPendingRevokeId(null)}
                          disabled={revokeMut.isPending}
                          aria-disabled={revokeMut.isPending}
                          className="text-xs px-3 min-h-[36px] rounded-md bg-white/5 hover:bg-white/10 text-neutral-300 disabled:opacity-50 transition-colors"
                        >
                          取消
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setPendingRevokeId(row.id)}
                        className="text-xs px-3 min-h-[36px] rounded-md text-red-300 hover:text-red-200 hover:bg-red-500/10 transition-colors"
                      >
                        撤销
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
          {/* 桌面端表格 */}
          <div className="hidden md:block overflow-x-auto [-webkit-overflow-scrolling:touch]">
            <table className="w-full text-sm">
              <thead className="text-xs uppercase tracking-wider text-[var(--fg-1)] border-b border-white/10">
                <tr>
                  <th className="text-left py-3 px-4 font-medium">链接</th>
                  <th className="text-left py-3 px-4 font-medium">Email</th>
                  <th className="text-left py-3 px-4 font-medium">Role</th>
                  <th className="text-left py-3 px-4 font-medium">状态</th>
                  <th className="text-left py-3 px-4 font-medium">过期</th>
                  <th className="text-left py-3 px-4 font-medium">创建</th>
                  <th className="text-right py-3 px-4 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => {
                  const st = statusOf(row, now);
                  const canRevoke = st === "valid";
                  const isConfirming = pendingRevokeId === row.id;
                  return (
                    <motion.tr
                      key={row.id}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{
                        duration: 0.18,
                        delay: Math.min(i * 0.03, 0.2),
                      }}
                      className="border-t border-white/5 hover:bg-white/[0.03] transition-colors align-middle"
                    >
                      <td className="py-3 px-4 max-w-[280px]">
                        <div className="flex items-center gap-2">
                          <code className="text-xs font-mono text-neutral-300 truncate">
                            {row.url}
                          </code>
                          <button
                            type="button"
                            onClick={() => onCopy(`row:${row.id}`, row.url)}
                            className="shrink-0 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[11px] text-neutral-400 hover:text-neutral-100 hover:bg-white/5 transition-colors"
                            aria-label="复制链接"
                          >
                            {copiedKey === `row:${row.id}` ? (
                              <>
                                <Check className="w-3 h-3 text-emerald-300" />
                                已复制
                              </>
                            ) : (
                              <>
                                <Copy className="w-3 h-3" />
                                复制
                              </>
                            )}
                          </button>
                        </div>
                      </td>
                      <td className="py-3 px-4 text-neutral-300">
                        {row.email ?? "—"}
                      </td>
                      <td className="py-3 px-4">
                        <RoleBadge role={row.role} />
                      </td>
                      <td className="py-3 px-4">
                        <StatusBadge status={st} usedBy={row.used_by_email} />
                      </td>
                      <td className="py-3 px-4 text-neutral-400 font-mono text-xs tabular-nums">
                        {row.expires_at ? formatISODate(row.expires_at) : "永久"}
                      </td>
                      <td className="py-3 px-4 text-neutral-400 font-mono text-xs tabular-nums">
                        {formatISODate(row.created_at)}
                      </td>
                      <td className="py-3 px-4 text-right">
                        {!canRevoke ? (
                          <span className="text-xs text-neutral-600">—</span>
                        ) : isConfirming ? (
                          <motion.div
                            initial={{ opacity: 0, scale: 0.96 }}
                            animate={{ opacity: 1, scale: 1 }}
                            className="inline-flex items-center gap-2"
                          >
                            <span className="text-xs text-neutral-400">
                              撤销?
                            </span>
                            <button
                              type="button"
                              onClick={() => revokeMut.mutate(row.id)}
                              disabled={revokeMut.isPending}
                              className="text-xs px-2 py-1 rounded-md bg-red-500/80 hover:bg-red-500 text-white disabled:opacity-50 transition-colors"
                            >
                              {revokeMut.isPending ? "撤销中" : "撤销"}
                            </button>
                            <button
                              type="button"
                              onClick={() => setPendingRevokeId(null)}
                              disabled={revokeMut.isPending}
                              className="text-xs px-2 py-1 rounded-md bg-white/5 hover:bg-white/10 text-neutral-300 disabled:opacity-50 transition-colors"
                            >
                              取消
                            </button>
                          </motion.div>
                        ) : (
                          <button
                            type="button"
                            onClick={() => setPendingRevokeId(row.id)}
                            className="text-xs text-red-300 hover:text-red-200 transition-colors"
                          >
                            撤销
                          </button>
                        )}
                      </td>
                    </motion.tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          </>
        )}
      </div>

      {revokeMut.isError && (
        <p className="flex items-center gap-1.5 text-sm text-red-300">
          <AlertCircle className="w-4 h-4" />
          撤销失败：{revokeMut.error?.message ?? "未知错误"}
        </p>
      )}
    </section>
  );
}

function FormField({
  id,
  label,
  children,
}: {
  id: string;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label
        htmlFor={id}
        className="text-[11px] font-medium uppercase tracking-wider text-[var(--fg-1)]"
      >
        {label}
      </label>
      {children}
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-neutral-500">
        {label}
      </div>
      <div className="mt-0.5">{children}</div>
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

function StatusBadge({
  status,
  usedBy,
}: {
  status: InviteStatus;
  usedBy: string | null;
}) {
  if (status === "valid") {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs bg-emerald-500/10 text-emerald-300 border border-emerald-500/30">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 shadow-[0_0_6px_currentColor]" />
        valid
      </span>
    );
  }
  if (status === "used") {
    return (
      <span
        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs bg-sky-500/10 text-sky-300 border border-sky-500/30"
        title={usedBy ? `被 ${usedBy} 使用` : undefined}
      >
        <span className="w-1.5 h-1.5 rounded-full bg-sky-400" />
        used
      </span>
    );
  }
  if (status === "revoked") {
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs bg-red-500/10 text-red-300 border border-red-500/30">
        <span className="w-1.5 h-1.5 rounded-full bg-red-400" />
        revoked
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs bg-neutral-500/10 text-neutral-400 border border-neutral-500/30">
      <span className="w-1.5 h-1.5 rounded-full bg-neutral-500" />
      expired
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

// ——— "now" 外部数据源（避免 react-hooks/purity 报错） ———

function subscribeTime(onChange: () => void): () => void {
  // P2-3：从 30s 降为 60s，减少不必要的 re-render 风暴；过期时间边界仍可由用户手动刷新感知
  const t = setInterval(onChange, 60_000);
  return () => clearInterval(t);
}

function getNow(): number {
  return Date.now();
}

function getNowSSR(): number {
  return 0;
}
