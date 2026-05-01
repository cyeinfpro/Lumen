"use client";

// /settings/prompts —— 独立嵌入式系统提示词管理页。

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { SystemPromptManager } from "@/components/ui/SystemPromptManager";

export default function PromptsPage() {
  return (
    <main className="min-h-[100dvh] w-full flex-1 bg-[var(--bg-0)] text-neutral-200">
      <div className="mx-auto flex max-w-6xl flex-col gap-5 px-4 py-6 md:px-8 md:py-10 safe-x mobile-compact">
        <header className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight md:text-3xl">
              系统提示词
            </h1>
            <p className="mt-1.5 text-sm text-[var(--fg-1)]">
              管理可复用的系统提示词方案。
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
    </main>
  );
}
