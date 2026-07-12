"use client";

import { ProvidersPanel } from "@/app/admin/_panels/ProvidersPanel";
import { SettingsShell } from "@/components/ui/shell/SettingsShell";

export default function ProvidersSettingsPage() {
  return (
    <SettingsShell
      title="供应商池"
      subtitle="本机 OpenAI 兼容端点、权重、并发与探活"
    >
      <section className="min-w-0 overflow-hidden pb-4 [&_button]:min-h-11 [&_input]:min-h-11 [&_select]:min-h-11 [&_textarea]:min-h-32 [&_textarea]:scroll-mb-32 [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_code]:break-all">
        <ProvidersPanel />
      </section>
    </SettingsShell>
  );
}
