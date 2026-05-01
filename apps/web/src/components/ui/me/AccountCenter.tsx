"use client";

import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  BarChart3,
  FileText,
  LogOut,
  Lock,
  Shield,
  Zap,
} from "lucide-react";

import { ActionSheet } from "@/components/ui/primitives/mobile";
import {
  getMe,
  logout,
  type AuthUser,
} from "@/lib/apiClient";
import { useSystemPromptsQuery } from "@/lib/queries";
import { logWarn } from "@/lib/logger";
import { useChatStore } from "@/store/useChatStore";

import { AccountRow } from "./AccountRow";

type AuthUserMaybeAdmin = AuthUser & { role?: "admin" | "member" };

const APP_VERSION = "v1.0.3";

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-4 pt-6 pb-2 text-[11px] font-semibold uppercase tracking-[0.1em] text-[var(--fg-2)]">
      {children}
    </div>
  );
}

export function AccountCenter() {
  const router = useRouter();
  const qc = useQueryClient();

  const meQuery = useQuery<AuthUserMaybeAdmin>({
    queryKey: ["me"],
    queryFn: () => getMe() as Promise<AuthUserMaybeAdmin>,
    retry: false,
    staleTime: 60_000,
  });
  const isAdmin = meQuery.data?.role === "admin";

  const promptsQuery = useSystemPromptsQuery();
  const promptCount = promptsQuery.data?.items?.length ?? 0;

  const storeFast = useChatStore((s) => s.composer?.fast ?? false);
  const setStoreFast = useChatStore((s) => s.setFast);
  const fast = storeFast;
  const toggleFast = (next: boolean) => {
    setStoreFast(next);
  };

  const [logoutOpen, setLogoutOpen] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);

  const handleLogout = async () => {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      await logout();
    } catch (err) {
      logWarn("mobile_me.logout_failed", { scope: "mobile-me", extra: { err: String(err) } });
    } finally {
      qc.clear();
      setLoggingOut(false);
      router.push("/login");
    }
  };

  return (
    <div className="flex flex-col">
      <SectionLabel>设置</SectionLabel>
      <div className="mx-4 rounded-2xl border border-[var(--border-subtle)] overflow-hidden">
        <AccountRow
          href="/settings/usage"
          icon={<BarChart3 className="w-4 h-4" />}
          label="用量统计"
          grouped
        />
        <AccountRow
          href="/settings/privacy"
          icon={<Lock className="w-4 h-4" />}
          label="隐私 & 数据"
          grouped
        />
        <AccountRow
          href="/settings/prompts"
          icon={<FileText className="w-4 h-4" />}
          label="系统提示词"
          badge={promptCount > 0 ? promptCount : undefined}
          grouped
        />
        <AccountRow
          icon={
            <Zap
              className="w-4 h-4"
              style={{ color: fast ? "var(--amber-400)" : undefined }}
            />
          }
          label="Fast 模式"
          toggle={{
            checked: fast,
            onChange: toggleFast,
            ariaLabel: "Fast 模式",
          }}
          grouped
          last
        />
      </div>

      {isAdmin && (
        <>
          <SectionLabel>管理</SectionLabel>
          <div className="mx-4 rounded-2xl border border-[var(--border-subtle)] overflow-hidden">
            <AccountRow
              href="/admin"
              icon={<Shield className="w-4 h-4" />}
              label="管理面板"
              grouped
              last
            />
          </div>
        </>
      )}

      <div className="mt-5 mx-4 rounded-2xl border border-[var(--border-subtle)] overflow-hidden">
        <AccountRow
          icon={<LogOut className="w-4 h-4" />}
          label="退出登录"
          destructive
          onClick={() => setLogoutOpen(true)}
          grouped
          last
        />
      </div>

      <div className="flex items-center justify-center gap-1.5 pt-6 pb-4">
        <span className="text-[11px] text-[var(--fg-2)]">Lumen</span>
        <span className="text-[11px] font-mono text-[var(--fg-2)]/60">{APP_VERSION}</span>
      </div>

      <ActionSheet
        open={logoutOpen}
        onClose={() => setLogoutOpen(false)}
        title="确认退出登录?"
        description="退出后需要重新登录才能继续使用"
        actions={[
          {
            key: "logout",
            label: loggingOut ? "正在退出…" : "退出登录",
            destructive: true,
            disabled: loggingOut,
            onSelect: () => void handleLogout(),
          },
        ]}
      />
    </div>
  );
}
