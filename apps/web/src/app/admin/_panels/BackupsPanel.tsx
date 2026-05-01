"use client";

// 备份与恢复面板：
// - 顶部"立即备份"按钮（同步跑 pg_dump + redis BGSAVE，通常几秒）
// - 备份点列表：timestamp、PG 大小、Redis 大小、恢复按钮
// - 恢复按钮点击后弹出二次确认 modal，输入"恢复"才能确认
// - 恢复触发后显示提示：服务会短暂不可用，~60 秒后刷新页面

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AnimatePresence, motion } from "framer-motion";
import { format, formatDistanceToNow } from "date-fns";
import { zhCN } from "date-fns/locale";
import {
  AlertTriangle,
  Archive,
  Clock,
  Database,
  HardDriveDownload,
  Loader2,
  RotateCcw,
  X,
} from "lucide-react";

import {
  ApiError,
  backupNow,
  listBackups,
  restoreBackup,
  type BackupItem,
} from "@/lib/apiClient";
import { EmptyBlock, ErrorBlock, ListSkeleton } from "../page";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatTs(ts: string, iso: string): string {
  try {
    return format(new Date(iso), "yyyy-MM-dd HH:mm:ss");
  } catch {
    return ts;
  }
}

function relativeFromIso(iso: string): string {
  try {
    return formatDistanceToNow(new Date(iso), { addSuffix: true, locale: zhCN });
  } catch {
    return "";
  }
}

export function BackupsPanel() {
  const qc = useQueryClient();
  const q = useQuery<{ items: BackupItem[]; total: number }>({
    queryKey: ["admin", "backups"],
    queryFn: () => listBackups(),
    // 4h 定时备份，10 分钟刷新一次即可
    refetchInterval: 10 * 60 * 1000,
  });

  const [banner, setBanner] = useState<
    { kind: "info" | "success" | "error"; text: string } | null
  >(null);
  const [restoreTarget, setRestoreTarget] = useState<BackupItem | null>(null);

  const backupMut = useMutation({
    mutationFn: backupNow,
    onSuccess: (r) => {
      if (r.ok) {
        setBanner({
          kind: "success",
          text: r.timestamp
            ? `已生成备份 ${r.timestamp}`
            : "备份已完成",
        });
        qc.invalidateQueries({ queryKey: ["admin", "backups"] });
      } else {
        setBanner({
          kind: "error",
          text: r.stderr_tail
            ? `备份失败：${r.stderr_tail.slice(-200)}`
            : "备份失败",
        });
      }
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.message : String(err);
      setBanner({ kind: "error", text: `备份失败：${msg}` });
    },
  });

  const restoreMut = useMutation({
    mutationFn: (ts: string) => restoreBackup(ts),
    onSuccess: (r) => {
      setRestoreTarget(null);
      setBanner({
        kind: "info",
        text: `已提交恢复任务（${r.timestamp}）：${r.note}`,
      });
    },
    onError: (err) => {
      const msg = err instanceof ApiError ? err.message : String(err);
      setBanner({ kind: "error", text: `触发恢复失败：${msg}` });
    },
  });

  const items = q.data?.items ?? [];

  return (
    <section className="space-y-5">
      {/* —— 顶部：立即备份 + 说明 —— */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-white/10 rounded-2xl p-4 md:p-5">
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
          <div className="flex items-start gap-3 min-w-0">
            <div className="w-9 h-9 rounded-xl bg-[var(--color-lumen-amber)]/15 border border-[var(--color-lumen-amber)]/25 flex items-center justify-center shrink-0">
              <Archive className="w-4 h-4 text-[var(--color-lumen-amber)]" />
            </div>
            <div className="min-w-0">
              <p className="text-sm text-neutral-100 font-medium">
                备份与恢复
              </p>
              <p className="text-xs text-neutral-500 mt-1 leading-relaxed break-words">
                系统每 4 小时自动备份 Postgres + Redis 到
                <code className="mx-1 px-1.5 py-0.5 rounded bg-white/5 border border-white/10 text-[11px] break-all">
                  /opt/lumendata/backup
                </code>
                ，最多保留 40 份。恢复会成对还原 PG 和 Redis。
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => backupMut.mutate()}
            disabled={backupMut.isPending}
            className="inline-flex items-center justify-center gap-1.5 w-full sm:w-auto min-h-[44px] sm:h-9 px-4 rounded-xl bg-[var(--color-lumen-amber)] hover:brightness-110 active:scale-[0.97] text-black text-sm font-medium disabled:opacity-50 transition-all"
          >
            {backupMut.isPending ? (
              <>
                <Loader2 className="w-3.5 h-3.5 animate-spin" /> 备份中
              </>
            ) : (
              <>
                <HardDriveDownload className="w-3.5 h-3.5" /> 立即备份
              </>
            )}
          </button>
        </div>
      </div>

      {/* —— banner —— */}
      <AnimatePresence>
        {banner && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className={
              "flex items-start gap-3 px-4 py-3 rounded-xl border " +
              (banner.kind === "success"
                ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-200"
                : banner.kind === "error"
                  ? "bg-red-500/10 border-red-500/30 text-red-200"
                  : "bg-sky-500/10 border-sky-500/30 text-sky-200")
            }
          >
            <p className="text-sm flex-1 break-words">{banner.text}</p>
            <button
              type="button"
              onClick={() => setBanner(null)}
              aria-label="关闭提示"
              className="shrink-0 w-5 h-5 inline-flex items-center justify-center rounded-md hover:bg-white/10 transition-colors"
            >
              <X className="w-3 h-3" />
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      {/* —— 列表 —— */}
      <div className="bg-[var(--bg-1)]/60 backdrop-blur-sm border border-white/10 rounded-2xl overflow-hidden">
        {q.isLoading ? (
          <ListSkeleton rows={4} />
        ) : q.isError ? (
          <ErrorBlock
            message={q.error?.message ?? "未知错误"}
            onRetry={() => void q.refetch()}
          />
        ) : items.length === 0 ? (
          <EmptyBlock
            title="暂无备份"
            description="点击右上角“立即备份”生成第一份，或等待下次自动备份"
          />
        ) : (
          <>
            {/* 桌面端表格 */}
            <div className="hidden md:block overflow-x-auto [-webkit-overflow-scrolling:touch]">
              <table className="w-full text-sm">
                <thead className="text-xs uppercase tracking-wider text-[var(--fg-1)] border-b border-white/10">
                  <tr>
                    <th className="text-left py-3 px-4 font-medium">时间</th>
                    <th className="text-left py-3 px-4 font-medium">相对</th>
                    <th className="text-right py-3 px-4 font-medium">
                      <span className="inline-flex items-center gap-1">
                        <Database className="w-3 h-3" /> PG
                      </span>
                    </th>
                    <th className="text-right py-3 px-4 font-medium">Redis</th>
                    <th className="text-right py-3 px-4 font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((b, i) => (
                    <motion.tr
                      key={b.timestamp}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{
                        duration: 0.18,
                        delay: Math.min(i * 0.02, 0.2),
                      }}
                      className="border-t border-white/5 hover:bg-white/[0.03] transition-colors"
                    >
                      <td className="py-3 px-4 text-neutral-100 font-mono text-xs tabular-nums">
                        {formatTs(b.timestamp, b.created_at)}
                      </td>
                      <td className="py-3 px-4 text-neutral-400 text-xs">
                        <span className="inline-flex items-center gap-1">
                          <Clock className="w-3 h-3" />
                          {relativeFromIso(b.created_at)}
                        </span>
                      </td>
                      <td className="py-3 px-4 text-right text-neutral-200 font-mono tabular-nums text-xs">
                        {formatBytes(b.pg_size)}
                      </td>
                      <td className="py-3 px-4 text-right text-neutral-200 font-mono tabular-nums text-xs">
                        {formatBytes(b.redis_size)}
                      </td>
                      <td className="py-3 px-4 text-right">
                        <button
                          type="button"
                          onClick={() => setRestoreTarget(b)}
                          className="inline-flex items-center gap-1 px-2 py-1 text-xs text-[var(--color-lumen-amber)] hover:brightness-110 transition-colors rounded-md hover:bg-[var(--color-lumen-amber)]/10"
                        >
                          <RotateCcw className="w-3 h-3" /> 恢复
                        </button>
                      </td>
                    </motion.tr>
                  ))}
                </tbody>
              </table>
            </div>
            {/* 移动端卡片列表 */}
            <ul className="md:hidden divide-y divide-white/5">
              {items.map((b) => (
                <li
                  key={b.timestamp}
                  className="p-3 border border-white/10 rounded-lg m-2 space-y-2"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm text-neutral-100 font-mono tabular-nums break-all">
                        {formatTs(b.timestamp, b.created_at)}
                      </div>
                      <div className="text-xs text-neutral-500 inline-flex items-center gap-1 mt-1">
                        <Clock className="w-3 h-3" />
                        {relativeFromIso(b.created_at)}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => setRestoreTarget(b)}
                      className="shrink-0 inline-flex items-center gap-1 text-sm px-3 min-h-[40px] rounded-md bg-[var(--color-lumen-amber)]/15 border border-[var(--color-lumen-amber)]/40 text-[var(--color-lumen-amber)] hover:brightness-110 transition-colors"
                    >
                      <RotateCcw className="w-3.5 h-3.5" /> 恢复
                    </button>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div className="rounded-md bg-white/[0.03] border border-white/5 px-2 py-1.5">
                      <div className="text-[11px] uppercase tracking-wider text-neutral-500 inline-flex items-center gap-1">
                        <Database className="w-2.5 h-2.5" /> PG
                      </div>
                      <div className="text-sm text-neutral-200 font-mono tabular-nums break-all">
                        {formatBytes(b.pg_size)}
                      </div>
                    </div>
                    <div className="rounded-md bg-white/[0.03] border border-white/5 px-2 py-1.5">
                      <div className="text-[11px] uppercase tracking-wider text-neutral-500">
                        Redis
                      </div>
                      <div className="text-sm text-neutral-200 font-mono tabular-nums break-all">
                        {formatBytes(b.redis_size)}
                      </div>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>

      {/* —— 恢复确认 modal —— */}
      <AnimatePresence>
        {restoreTarget && (
          <RestoreModal
            target={restoreTarget}
            pending={restoreMut.isPending}
            onCancel={() => {
              if (!restoreMut.isPending) setRestoreTarget(null);
            }}
            onConfirm={() => restoreMut.mutate(restoreTarget.timestamp)}
          />
        )}
      </AnimatePresence>
    </section>
  );
}

function RestoreModal({
  target,
  pending,
  onCancel,
  onConfirm,
}: {
  target: BackupItem;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [confirmText, setConfirmText] = useState("");
  const canConfirm = confirmText.trim() === "恢复" && !pending;

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-[80] flex items-end sm:items-center justify-center p-0 sm:p-4 bg-black/60 backdrop-blur-sm"
      onClick={onCancel}
    >
      <motion.div
        initial={{ opacity: 0, y: 12, scale: 0.97 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 12, scale: 0.97 }}
        transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
        className="w-full max-w-md rounded-t-2xl sm:rounded-2xl bg-neutral-900/98 backdrop-blur-xl border border-white/12 border-b-0 sm:border-b shadow-2xl p-5 pb-[calc(1.25rem+env(safe-area-inset-bottom))] sm:pb-5"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-labelledby="restore-title"
      >
        <div className="flex items-start gap-3">
          <div className="w-9 h-9 rounded-xl bg-red-500/15 border border-red-500/30 flex items-center justify-center shrink-0">
            <AlertTriangle className="w-4 h-4 text-red-300" />
          </div>
          <div className="flex-1 min-w-0">
            <h3 id="restore-title" className="text-base font-semibold text-neutral-50">
              恢复备份？
            </h3>
            <p className="text-xs text-neutral-400 mt-1 leading-relaxed">
              将把 Postgres 与 Redis 还原到{" "}
              <span className="text-neutral-200 font-mono">
                {formatTs(target.timestamp, target.created_at)}
              </span>{" "}
              的快照。
              <br />
              <span className="text-red-300">
                此后的对话、生成记录会被丢弃；服务会短暂不可用（约 30–60 秒），完成后需刷新页面。
              </span>
            </p>
            <div className="mt-4 space-y-2">
              <label
                htmlFor="restore-confirm"
                className="block text-xs text-neutral-400"
              >
                输入 <span className="font-semibold text-neutral-200">&ldquo;恢复&rdquo;</span> 二字后点击确认
              </label>
              <input
                id="restore-confirm"
                type="text"
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                disabled={pending}
                autoFocus
                className="w-full h-9 px-3 rounded-lg bg-[var(--bg-0)]/60 border border-white/10 focus:outline-none focus:border-red-500/40 focus:ring-2 focus:ring-red-500/20 text-sm disabled:opacity-50"
                placeholder="恢复"
              />
            </div>
            <div className="mt-5 flex flex-col-reverse sm:flex-row sm:items-center sm:justify-end gap-2 sm:gap-2">
              <button
                type="button"
                onClick={onCancel}
                disabled={pending}
                className="min-h-[44px] sm:h-9 px-4 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-sm text-neutral-200 disabled:opacity-50 transition-colors"
              >
                取消
              </button>
              <button
                type="button"
                onClick={onConfirm}
                disabled={!canConfirm}
                className="inline-flex items-center justify-center gap-1.5 min-h-[44px] sm:h-9 px-4 rounded-lg bg-red-500 hover:brightness-110 text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed transition-all"
              >
                {pending ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" /> 触发中
                  </>
                ) : (
                  "确认恢复"
                )}
              </button>
            </div>
          </div>
        </div>
      </motion.div>
    </motion.div>
  );
}
