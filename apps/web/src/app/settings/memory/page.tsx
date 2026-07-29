"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { SettingsShell } from "@/components/ui/shell/SettingsShell";

import { MemoryCapabilityModal } from "./modal/MemoryCapabilityModal";
import { ManualMemorySection } from "./sections/ManualMemorySection";
import { MemoryLibrarySection } from "./sections/MemoryLibrarySection";
import {
  MemoryCapabilityBanner,
  MemoryFirstRunCard,
  MemorySettingsToggles,
} from "./sections/MemoryOverviewSections";
import { MemoryScopeSidebar } from "./sections/MemoryScopeSidebar";
import { MemoryStagingSection } from "./sections/MemoryStagingSection";
import { MemoryTimelineAndClear } from "./sections/MemoryTimelineAndClear";
import { useMemoryPageModel } from "./useMemoryPageModel";

export default function MemorySettingsPage() {
  const model = useMemoryPageModel();

  return (
    <SettingsShell title="记忆" subtitle="MEMORY" maxWidth="max-w-6xl">
      <div className="space-y-5 pb-4 sm:space-y-6">
        <header className="hidden items-start justify-between gap-4 md:flex">
          <div>
            <h1 className="type-page-title">记忆</h1>
            <p className="type-body mt-1.5">
              管理账号级长期记忆、候选建议和最近变化。
            </p>
          </div>
          <Link
            href="/me"
            className="inline-flex min-h-9 items-center gap-1.5 px-2 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)]"
          >
            <ArrowLeft className="h-4 w-4" />
            返回我的
          </Link>
        </header>

        <MemoryCapabilityBanner {...model.capabilityBanner} />
        <MemorySettingsToggles {...model.settingsToggles} />
        <MemoryFirstRunCard {...model.firstRun} />

        <section className="grid min-w-0 gap-5 lg:grid-cols-[240px_minmax(0,1fr)]">
          <MemoryScopeSidebar {...model.scopeSidebar} />

          <div className="min-w-0 space-y-5">
            <ManualMemorySection {...model.manualMemory} />
            <MemoryLibrarySection {...model.memoryLibrary} />
            <MemoryStagingSection {...model.memoryStaging} />
            <MemoryTimelineAndClear {...model.timelineAndClear} />
          </div>
        </section>
      </div>
      <MemoryCapabilityModal {...model.capabilityModal} />
    </SettingsShell>
  );
}
