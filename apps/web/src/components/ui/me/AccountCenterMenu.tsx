"use client";

import {
  BarChart3,
  Brain,
  CreditCard,
  FileText,
  KeyRound,
  LogOut,
  Lock,
  Shield,
  Zap,
} from "lucide-react";

import { ActionSheet } from "@/components/ui/primitives/mobile";
import pkg from "../../../../package.json";

import { AccountRow } from "./AccountRow";
import type { AccountMode } from "./accountCenterModel";
import type { AccountLogoutController } from "./useAccountLogout";

const rawAppVersion = process.env.NEXT_PUBLIC_LUMEN_VERSION ?? pkg.version;
const APP_VERSION = rawAppVersion.startsWith("v")
  ? rawAppVersion
  : `v${rawAppVersion}`;

interface AccountCenterMenuProps {
  accountMode: AccountMode;
  isAdmin: boolean;
  walletBalance?: string;
  promptCount: number;
  stagingCount: number;
  fast: boolean;
  onFastChange: (next: boolean) => void;
  logout: AccountLogoutController;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-4 pb-2 pt-6 type-overline text-[var(--fg-2)]">
      {children}
    </div>
  );
}

function visibleBadge(count: number): number | undefined {
  return count > 0 ? count : undefined;
}

export function AccountCenterMenu({
  accountMode,
  isAdmin,
  walletBalance,
  promptCount,
  stagingCount,
  fast,
  onFastChange,
  logout,
}: AccountCenterMenuProps) {
  return (
    <div className="flex flex-col">
      <SectionLabel>设置</SectionLabel>
      <div className="mx-4 overflow-hidden border-y border-[var(--border-subtle)]">
        <AccountRow
          href="/settings/usage"
          icon={<BarChart3 className="h-4 w-4" />}
          label="用量统计"
          grouped
        />
        {accountMode === "wallet" ? (
          <AccountRow
            href="/me/wallet"
            icon={<CreditCard className="h-4 w-4" />}
            label="钱包"
            description={walletBalance}
            grouped
          />
        ) : (
          <AccountRow
            href="/settings/api-key"
            icon={<KeyRound className="h-4 w-4" />}
            label="API Key"
            grouped
          />
        )}
        <AccountRow
          href="/settings/privacy"
          icon={<Lock className="h-4 w-4" />}
          label="隐私 & 数据"
          grouped
        />
        <AccountRow
          href="/settings/memory"
          icon={<Brain className="h-4 w-4" />}
          label="记忆"
          badge={visibleBadge(stagingCount)}
          grouped
        />
        <AccountRow
          href="/settings/prompts"
          icon={<FileText className="h-4 w-4" />}
          label="系统提示词"
          badge={visibleBadge(promptCount)}
          grouped
        />
        <AccountRow
          icon={
            <Zap
              className="h-4 w-4"
              style={{ color: fast ? "var(--amber-400)" : undefined }}
            />
          }
          label="Fast 模式"
          toggle={{
            checked: fast,
            onChange: onFastChange,
            ariaLabel: "Fast 模式",
          }}
          grouped
          last
        />
      </div>

      <AdminAccountSection visible={isAdmin} />

      <div className="mx-4 mt-5 overflow-hidden border-y border-[var(--border-subtle)]">
        <AccountRow
          icon={<LogOut className="h-4 w-4" />}
          label="退出登录"
          destructive
          onClick={logout.request}
          grouped
          last
        />
      </div>

      <div className="flex items-center justify-center gap-1.5 pb-4 pt-6">
        <span className="text-[11px] text-[var(--fg-2)]">Lumen</span>
        <span className="font-mono text-[11px] text-[var(--fg-2)]/60">
          {APP_VERSION}
        </span>
      </div>

      <ActionSheet
        open={logout.isOpen}
        onClose={logout.dismiss}
        title="确认退出登录?"
        description="退出后需要重新登录才能继续使用"
        actions={[
          {
            key: "logout",
            label: logout.isPending ? "正在退出…" : "退出登录",
            destructive: true,
            disabled: logout.isPending,
            onSelect: () => void logout.confirm(),
          },
        ]}
      />
    </div>
  );
}

function AdminAccountSection({ visible }: { visible: boolean }) {
  if (!visible) return null;
  return (
    <>
      <SectionLabel>管理</SectionLabel>
      <div className="mx-4 overflow-hidden border-y border-[var(--border-subtle)]">
        <AccountRow
          href="/admin"
          icon={<Shield className="h-4 w-4" />}
          label="管理面板"
          grouped
          last
        />
      </div>
    </>
  );
}
