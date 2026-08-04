"use client";

// AccountSheet · 移动版设置弹出面板
// - 由 /me 顶栏齿轮按钮 / 用户卡片点击触发
// - 高度 snap 到 88%（足够容纳所有设置项 + 退出登录）
// - 内部 link 跳转后路由变化会自动卸载 sheet
// - 退出登录二次确认仍走 AccountCenter 自带的 ActionSheet（嵌套 OK：z-dialog 后渲染覆盖前者）

import { BottomSheet } from "@/components/ui/primitives/mobile";
import { Avatar } from "@/components/ui/primitives";
import { Mail } from "lucide-react";
import { cn } from "@/lib/utils";
import { AccountCenter } from "./AccountCenter";

interface AccountSheetProps {
  open: boolean;
  onClose: () => void;
  user?: {
    name?: string | null;
    email?: string | null;
  } | null;
  loading?: boolean;
}

export function AccountSheet({ open, onClose, user, loading }: AccountSheetProps) {
  const userLabel = user?.name || user?.email || "";
  const avatarChar = userLabel ? userLabel.slice(0, 1).toUpperCase() : "U";

  return (
    <BottomSheet
      open={open}
      onClose={onClose}
      ariaLabel="账户与设置"
      snapPoints={["88%", "60%"]}
      defaultSnapIndex={0}
    >
      {/* sheet header：用户摘要（粘性置顶，避免滑动后看不到自己是谁） */}
      <div
        className={cn(
          "sticky top-0 z-[var(--z-header)]",
          "bg-[var(--bg-1)]/95 backdrop-blur-xl",
          "border-b border-[var(--border-subtle)]",
        )}
      >
        <div className="flex items-center gap-3 px-4 py-3">
          <Avatar
            name={userLabel}
            initials={avatarChar}
            alt={userLabel || "账户头像"}
            size="md"
            className="border-accent-border bg-accent text-[var(--accent-on)]"
          />
          <div className="flex-1 min-w-0">
            {user?.name && (
              <p className="line-clamp-2 break-words type-card-title text-[var(--fg-0)]">
                {user.name}
              </p>
            )}
            {user?.email && (
              <p className="mt-0.5 flex min-w-0 items-start gap-1.5 type-caption text-[var(--fg-2)]">
                <Mail className="w-3 h-3 shrink-0" />
                <span className="min-w-0 break-all">{user.email}</span>
              </p>
            )}
            {loading && (
              <div className="space-y-1.5">
                <div className="h-3.5 w-24 rounded bg-[var(--bg-2)] animate-pulse" />
                <div className="h-3 w-32 rounded bg-[var(--bg-2)] animate-pulse" />
              </div>
            )}
          </div>
        </div>
      </div>

      {/* AccountCenter 复用：所有 link 在 router push 后路由变化会卸载 /me，sheet 随之关闭 */}
      <div className="pb-[max(0.5rem,env(safe-area-inset-bottom,0px))] pt-1">
        <AccountCenter />
      </div>
    </BottomSheet>
  );
}
