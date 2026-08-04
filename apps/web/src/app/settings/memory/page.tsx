"use client";

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
    <SettingsShell title="记忆" subtitle="记忆">
      <div
        className="page-frame space-y-5 pb-4 sm:space-y-6"
        data-width="settings"
      >
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
