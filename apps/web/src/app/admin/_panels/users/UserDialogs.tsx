"use client";

import { useRef, useState } from "react";
import NextImage from "next/image";
import { motion } from "framer-motion";
import { Images, Loader2, X } from "lucide-react";

import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { useAdminUserHistoryQuery } from "@/lib/queries";
import type { AdminUserOut } from "@/lib/types";
import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";
import {
  EmptyBlock,
  ErrorBlock,
  ListSkeleton,
} from "../../_components/AdminFeedback";
import { formatISODate } from "../../_components/adminUi";

export function DeleteUserDescription({
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

export function UserHistoryDialog({
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

export function PasswordDialog({
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
