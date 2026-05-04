"use client";

// /settings/prompts —— 独立嵌入式系统提示词管理页。

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { SystemPromptManager } from "@/components/ui/SystemPromptManager";
import { SettingsShell } from "@/components/ui/shell/SettingsShell";

export default function PromptsPage() {
  return (
    <SettingsShell title="系统提示词" subtitle="PROMPTS">
      <div className="flex flex-col gap-5">
        <header className="hidden flex-wrap items-start justify-between gap-4 md:flex">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight md:text-3xl">
              系统提示词
            </h1>
            <p className="mt-1.5 text-sm text-[var(--fg-1)]">
              管理可复用的系统提示词。
            </p>
          </div>
          <Link
            href="/me"
            className="inline-flex items-center gap-1.5 text-sm text-neutral-400 transition-colors hover:text-neutral-100"
          >
            <ArrowLeft className="h-4 w-4" />
            返回我的
          </Link>
        </header>

        <SystemPromptManager mode="embedded" hideTrigger />
      </div>
    </SettingsShell>
  );
}
