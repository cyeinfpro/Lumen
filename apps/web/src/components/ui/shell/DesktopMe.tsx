"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Mail, Search, X } from "lucide-react";

import { DesktopTopNav } from "@/components/ui/shell";
import { AccountCenter } from "@/components/ui/me/AccountCenter";
import { ConversationList } from "@/components/ui/me/ConversationList";
import { Avatar, Card, IconButton } from "@/components/ui/primitives";
import { getMe, type AuthUser } from "@/lib/apiClient";
import { cn } from "@/lib/utils";

export function DesktopMe() {
  const [query, setQuery] = useState("");

  const meQuery = useQuery<AuthUser>({
    queryKey: ["me"],
    queryFn: getMe,
    retry: false,
    staleTime: 60_000,
  });

  const userLabel = meQuery.data?.name || meQuery.data?.email || "";
  const avatarChar = userLabel ? userLabel.slice(0, 1).toUpperCase() : "U";

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full flex-col bg-[var(--bg-0)]">
      <DesktopTopNav active="me" />

      <main className="min-h-0 flex-1 overflow-y-auto">
        <div
          className={cn(
            "page-frame grid grid-cols-1 gap-[var(--space-10)]",
            "lg:grid-cols-[1fr_320px]",
          )}
          data-width="settings"
        >
          {/* 左列：会话列表 */}
          <section aria-label="会话" className="min-w-0">
            <div className="flex items-center justify-between gap-4 mb-5">
              <h1 className="type-page-title-sm">
                会话
              </h1>
            </div>

            <div
              className={cn(
                "mb-5 flex min-h-11 items-center gap-2 px-3.5 md:min-h-10",
                "control-shell",
                "focus-within:border-accent-border",
                "transition-colors",
              )}
            >
              <Search className="w-4 h-4 text-[var(--fg-2)] shrink-0" />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索会话…"
                aria-label="搜索会话"
                className={cn(
                  "flex-1 bg-transparent border-none outline-none",
                  "h-10",
                  "type-body-sm text-[var(--fg-0)] placeholder:text-[var(--fg-2)]",
                )}
              />
              {query && (
                <IconButton
                  size="sm"
                  variant="ghost"
                  onClick={() => setQuery("")}
                  aria-label="清空"
                  className="w-6 h-6 max-sm:min-h-6 max-sm:min-w-6 rounded-full"
                >
                  <X className="w-3.5 h-3.5" />
                </IconButton>
              )}
            </div>
            <ConversationList query={query} />
          </section>

          {/* 右列：用户信息 + 账号中心 */}
          <aside aria-label="账号中心" className="min-w-0">
            <div className="lg:sticky lg:top-11">
              {/* 用户信息卡 */}
              <Card
                variant="default"
                padding="lg"
                className="flex flex-col items-center gap-3 mb-4"
              >
                <Avatar
                  size="lg"
                  name={userLabel || "Lumen 用户"}
                  initials={avatarChar}
                />
                {meQuery.data?.name && (
                  <p className="type-card-title truncate max-w-full">
                    {meQuery.data.name}
                  </p>
                )}
                {meQuery.data?.email && (
                  <p className="flex items-center gap-1.5 type-body-sm truncate max-w-full">
                    <Mail className="w-3 h-3 shrink-0" />
                    {meQuery.data.email}
                  </p>
                )}
                {meQuery.isLoading && (
                  <div className="flex flex-col items-center gap-2">
                    <div className="h-4 w-20 rounded bg-[var(--bg-2)] animate-pulse" />
                    <div className="h-3 w-32 rounded bg-[var(--bg-2)] animate-pulse" />
                  </div>
                )}
              </Card>

              <AccountCenter />
            </div>
          </aside>
        </div>
      </main>
    </div>
  );
}
