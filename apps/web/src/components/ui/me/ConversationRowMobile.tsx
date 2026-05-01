"use client";

// 移动端会话行：68px 高，左图标 + 标题/meta + 右 kebab 管理按钮。
// 左滑露出「改名 / 归档 / 删除」（SwipeRow，快捷手势）。
// 右侧 ••• 按钮显式呼出 ActionSheet，方便不会左滑的用户也能管理。
// 改名走 BottomSheet；归档与删除都有异步 mutation 回调，外部 handler 负责 invalidate。

import { formatDistanceToNowStrict } from "date-fns";
import { zhCN } from "date-fns/locale";
import {
  Archive,
  ArchiveRestore,
  Camera,
  MessageSquare,
  MoreHorizontal,
  Pencil,
  Trash2,
} from "lucide-react";
import { useMemo, useState } from "react";

import {
  ActionSheet,
  BottomSheet,
  SwipeRow,
} from "@/components/ui/primitives/mobile";
import type { ConversationSummary } from "@/lib/apiClient";
import { cn } from "@/lib/utils";

// ConversationSummary 当前没有 message_count / generation_count，但后端 spec 已规划返回。
// 先用宽容读法：有就展示，没有就降级。
type ConversationWithCounts = ConversationSummary & {
  message_count?: number;
  generation_count?: number;
  updated_at?: string;
};

export interface ConversationRowMobileProps {
  conv: ConversationSummary;
  active: boolean;
  onSelect: () => void;
  onRename: (title: string) => void;
  onArchive: () => void;
  onDelete: () => void;
}

function titleOf(c: ConversationSummary): string {
  const t = c.title?.trim();
  return t || "New Canvas";
}

function relativeTime(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  try {
    return formatDistanceToNowStrict(new Date(t), { locale: zhCN });
  } catch {
    return "";
  }
}

export function ConversationRowMobile({
  conv,
  active,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: ConversationRowMobileProps) {
  const withCounts = conv as ConversationWithCounts;
  const genCount = withCounts.generation_count ?? 0;
  const msgCount = withCounts.message_count ?? 0;
  const isImageConv = genCount > 0;

  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState("");
  const [actionsOpen, setActionsOpen] = useState(false);

  const meta = useMemo(() => {
    const parts: string[] = [];
    if (msgCount > 0) parts.push(`${msgCount} 条`);
    if (genCount > 0) parts.push(`${genCount} 张图`);
    return parts.join(" · ");
  }, [msgCount, genCount]);

  const timeIso =
    withCounts.updated_at || conv.last_activity_at || conv.created_at;

  const openRename = () => {
    setRenameValue(titleOf(conv));
    setRenameOpen(true);
  };

  const submitRename = () => {
    const next = renameValue.trim();
    setRenameOpen(false);
    if (!next || next === titleOf(conv)) return;
    onRename(next);
  };

  const Icon = isImageConv ? Camera : MessageSquare;

  return (
    <>
      <SwipeRow
        actions={[
          {
            key: "rename",
            label: "改名",
            icon: <Pencil className="w-4 h-4" />,
            color: "neutral",
            onAction: openRename,
          },
          {
            key: "archive",
            label: conv.archived ? "取消归档" : "归档",
            icon: <Archive className="w-4 h-4" />,
            color: "warning",
            onAction: onArchive,
          },
          {
            key: "delete",
            label: "删除",
            icon: <Trash2 className="w-4 h-4" />,
            color: "danger",
            confirm: true,
            onAction: onDelete,
          },
        ]}
      >
        <div
          className={cn(
            "relative w-full min-h-[68px] flex items-stretch",
            "border-b border-[var(--border-subtle)]",
            "transition-colors",
            active && "bg-[var(--amber-400)]/[0.05]",
          )}
        >
          {active && (
            <span
              aria-hidden
              className="absolute left-0 top-3 bottom-3 w-[3px] rounded-r bg-[var(--amber-400)] shadow-[0_0_8px_var(--amber-glow)]"
            />
          )}

          <button
            type="button"
            onClick={onSelect}
            aria-label={titleOf(conv)}
            aria-current={active ? "true" : undefined}
            className={cn(
              "flex-1 min-w-0 flex items-center gap-3.5 pl-4 pr-2 py-3 text-left",
              "active:bg-[var(--bg-2)] transition-colors",
            )}
          >
            <span
              aria-hidden
              className={cn(
                "inline-flex items-center justify-center w-11 h-11 rounded-xl shrink-0",
                isImageConv
                  ? "bg-[var(--amber-400)]/12 text-[var(--amber-400)]"
                  : "bg-[var(--bg-2)] text-[var(--fg-2)]",
              )}
            >
              <Icon className="w-[18px] h-[18px]" strokeWidth={1.8} />
            </span>

            <span className="flex-1 min-w-0 flex flex-col gap-1">
              <span
                className={cn(
                  "truncate text-[15px] leading-tight",
                  active
                    ? "font-semibold text-[var(--fg-0)]"
                    : "font-medium text-[var(--fg-0)]",
                )}
              >
                {titleOf(conv)}
              </span>
              <span className="flex items-center gap-1.5 text-[12px] text-[var(--fg-2)]">
                {meta && <span className="truncate">{meta}</span>}
                {meta && timeIso && <span aria-hidden>·</span>}
                {timeIso && (
                  <span className="font-mono tracking-wider shrink-0">
                    {relativeTime(timeIso)}
                  </span>
                )}
              </span>
            </span>
          </button>

          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setActionsOpen(true);
            }}
            aria-label="管理会话"
            aria-haspopup="menu"
            className={cn(
              "shrink-0 inline-flex items-center justify-center w-11 mr-1 my-2 rounded-full",
              "text-[var(--fg-2)] active:bg-[var(--bg-3)] active:scale-95",
              "transition-[background-color,transform] duration-150",
              "outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/40",
            )}
          >
            <MoreHorizontal className="w-4 h-4" />
          </button>
        </div>
      </SwipeRow>

      <ActionSheet
        open={actionsOpen}
        onClose={() => setActionsOpen(false)}
        title={titleOf(conv)}
        actions={[
          {
            key: "rename",
            label: "重命名",
            icon: <Pencil className="w-4 h-4" />,
            onSelect: openRename,
          },
          {
            key: "archive",
            label: conv.archived ? "取消归档" : "归档",
            icon: conv.archived ? (
              <ArchiveRestore className="w-4 h-4" />
            ) : (
              <Archive className="w-4 h-4" />
            ),
            onSelect: onArchive,
          },
          {
            key: "delete",
            label: "删除会话",
            icon: <Trash2 className="w-4 h-4" />,
            destructive: true,
            onSelect: onDelete,
          },
        ]}
      />

      <BottomSheet
        open={renameOpen}
        onClose={() => setRenameOpen(false)}
        ariaLabel="重命名会话"
      >
        <div className="px-4 pt-2 pb-6">
          <h3 className="text-[15px] font-medium text-[var(--fg-0)] mb-3">
            重命名会话
          </h3>
          <input
            autoFocus
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submitRename();
              } else if (e.key === "Escape") {
                setRenameOpen(false);
              }
            }}
            placeholder="输入新标题"
            className={cn(
              "w-full h-11 px-3 rounded-xl text-[15px]",
              "bg-[var(--bg-2)] border border-[var(--border-subtle)]",
              "text-[var(--fg-0)] placeholder:text-[var(--fg-2)]",
              "outline-none focus:border-[var(--amber-400)]/60",
            )}
          />
          <div className="mt-4 flex gap-2">
            <button
              type="button"
              onClick={() => setRenameOpen(false)}
              className="flex-1 h-11 rounded-xl bg-[var(--bg-2)] border border-[var(--border-subtle)] text-[14px] text-[var(--fg-1)] active:bg-[var(--bg-3)]"
            >
              取消
            </button>
            <button
              type="button"
              onClick={submitRename}
              disabled={!renameValue.trim()}
              className="flex-1 h-11 rounded-xl bg-[var(--amber-400)] text-black text-[14px] font-medium active:brightness-95 disabled:opacity-50"
            >
              确定
            </button>
          </div>
        </div>
      </BottomSheet>
    </>
  );
}
