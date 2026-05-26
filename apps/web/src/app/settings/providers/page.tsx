"use client";

import { ProvidersPanel } from "@/app/admin/_panels/ProvidersPanel";
import { SettingsShell } from "@/components/ui/shell/SettingsShell";

export default function ProvidersSettingsPage() {
  return (
    <SettingsShell
      title="供应商池"
      subtitle="本机 OpenAI 兼容端点、权重、并发与探活"
    >
      <ProvidersPanel />
    </SettingsShell>
  );
}
