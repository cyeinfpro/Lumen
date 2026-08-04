"use client";

// /settings/prompts —— 独立嵌入式系统提示词管理页。

import { SystemPromptManager } from "@/components/ui/SystemPromptManager";
import { SettingsShell } from "@/components/ui/shell/SettingsShell";

export default function PromptsPage() {
  return (
    <SettingsShell title="系统提示词" subtitle="提示词">
      <div
        className="page-frame flex min-w-0 flex-col gap-5 pb-4"
        data-width="settings"
      >
        <section className="min-w-0 overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/60 p-3 sm:p-5 [&_button]:min-h-11 [&_input]:min-h-11 [&_textarea]:min-h-32 [&_textarea]:scroll-mb-32">
          <SystemPromptManager mode="embedded" hideTrigger />
        </section>
      </div>
    </SettingsShell>
  );
}
