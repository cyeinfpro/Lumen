"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  AlertCircle,
  Check,
  Copy,
  ExternalLink,
  MessageCircle,
  RefreshCw,
} from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { SettingsShell } from "@/components/ui/shell/SettingsShell";
import { createTelegramLinkCode } from "@/lib/apiClient";
import type { TelegramLinkCodeOut } from "@/lib/types";
import { copy } from "@/lib/copy";

export default function TelegramSettingsPage() {
  const [copied, setCopied] = useState<string | null>(null);
  const [linkCode, setLinkCode] = useState<TelegramLinkCodeOut | null>(null);
  const mut = useMutation({
    mutationFn: createTelegramLinkCode,
    onSuccess: (out) => {
      setLinkCode(out);
      setCopied(null);
    },
  });

  const copyText = async (label: string, value: string) => {
    await navigator.clipboard.writeText(value);
    setCopied(label);
    window.setTimeout(() => setCopied((current) => (current === label ? null : current)), 1600);
  };

  return (
    <SettingsShell title="Telegram" subtitle="机器人">
      <div
        className="page-frame space-y-6 pb-4 sm:space-y-8"
        data-width="settings"
      >
        <Card variant="subtle" padding="lg" className="space-y-4 max-sm:p-4">
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]">
              <MessageCircle className="h-4 w-4" />
            </div>
            <div className="min-w-0 flex-1">
              <p className="type-card-title">绑定 Telegram 机器人</p>
              <p className="type-body-sm mt-1 text-[var(--fg-2)]">
                绑定码 10 分钟有效。重新生成后使用最新一组绑定码。
              </p>
            </div>
          </div>

          <Button
            variant="primary"
            size="md"
            onClick={() => mut.mutate()}
            disabled={mut.isPending}
            loading={mut.isPending}
            leftIcon={!mut.isPending ? <RefreshCw className="h-4 w-4" /> : undefined}
          >
            {linkCode ? "重新生成绑定码" : "生成绑定码"}
          </Button>

          {mut.isError && (
            <div className="flex items-start gap-2 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-3 py-2 type-body-sm text-[var(--danger-fg)]">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
              {mut.error instanceof Error ? mut.error.message : copy.error.unknown}
            </div>
          )}
        </Card>

        {linkCode && (
          <Card variant="subtle" padding="lg" className="space-y-4 max-sm:p-4">
            <div>
              <p className="type-caption font-medium text-[var(--fg-1)]">绑定码</p>
              <div className="mt-2 grid gap-2 sm:flex sm:flex-wrap sm:items-center">
                <code className="type-card-title min-h-11 min-w-0 overflow-x-auto rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2.5 font-mono tracking-wider text-[var(--fg-0)]">
                  {linkCode.code}
                </code>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void copyText("code", linkCode.code)}
                  leftIcon={<Copy className="h-3.5 w-3.5" />}
                >
                  {copied === "code" ? copy.state.copied : copy.action.copy}
                </Button>
              </div>
              <p className="mt-2 type-caption text-[var(--fg-2)]">
                有效期约 {Math.round(linkCode.expires_in / 60)} 分钟。
              </p>
            </div>

            <div className="grid gap-2 sm:flex sm:flex-wrap">
              {linkCode.deep_link && (
                <a
                  href={linkCode.deep_link}
                  target="_blank"
                  rel="noreferrer"
                  className="type-control inline-flex min-h-10 items-center justify-center gap-1.5 rounded-[var(--radius-control)] bg-[var(--accent)] px-3 font-medium text-[var(--accent-on)] max-sm:min-h-11"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  打开 Telegram
                </a>
              )}
              {linkCode.deep_link && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void copyText("link", linkCode.deep_link ?? "")}
                  leftIcon={<Copy className="h-3.5 w-3.5" />}
                >
                  {copied === "link" ? copy.state.copied : "复制链接"}
                </Button>
              )}
            </div>

            {copied && (
              <div className="flex items-center gap-2 rounded-[var(--radius-control)] border border-success-border bg-success-soft px-3 py-2 type-body-sm text-success">
                <Check className="h-4 w-4" />
                {copy.state.copied}
              </div>
            )}
          </Card>
        )}
      </div>
    </SettingsShell>
  );
}
