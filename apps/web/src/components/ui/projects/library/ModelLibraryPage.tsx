"use client";

// Editorial 重构：杂志大标题 + hairline tab + 去三层卡。
// /library 主壳：模特库 / 任务中心 / 新建模特 三 tab。
// 提交"新建模特"后自动切到"任务中心"。

import { motion } from "framer-motion";
import { useState } from "react";

import { toast } from "@/components/ui/primitives/Toast";
import { SPRING } from "@/lib/motion";
import { useGenerateApparelModelLibraryMutation } from "@/lib/queries";
import type { ApparelModelLibraryGenerateIn } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import {
  ProjectMobileTabBar,
  ProjectMobileTopBar,
  ProjectTopBar,
} from "../components/ProjectTopBar";
import { OnlineBanner } from "../components/OnlineBanner";
import { ModelLibraryBrowser } from "./ModelLibraryBrowser";
import { ModelLibraryGenerator } from "./ModelLibraryGenerator";
import { ModelLibraryJobsPanel } from "./ModelLibraryJobsPanel";

type LibraryTab = "browse" | "jobs" | "create";

const TABS: Array<{ key: LibraryTab; label: string; eyebrow: string }> = [
  { key: "browse", label: "模特库", eyebrow: "Browse" },
  { key: "jobs", label: "任务中心", eyebrow: "Jobs" },
  { key: "create", label: "新建模特", eyebrow: "Create" },
];

export function ModelLibraryPage() {
  const [tab, setTab] = useState<LibraryTab>("browse");
  const generate = useGenerateApparelModelLibraryMutation({
    onSuccess: () => {
      toast.success("已派发新建模特任务", {
        description: "已切到任务中心，可在这里查看进度",
      });
      setTab("jobs");
    },
    onError: (err) =>
      toast.error("派发失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  const handleGenerate = async (body: ApparelModelLibraryGenerateIn) => {
    await generate.mutateAsync(body);
  };

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="模特库"
        subtitle="LIBRARY · JOBS · CREATE"
      />
      <ProjectTopBar />

      <main className="lumen-studio-bg mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-12 pt-3 md:mb-0 md:px-10 md:py-6">
        <div className="mx-auto grid w-full max-w-[1440px] gap-8 md:gap-10">
          <Hero />

          <Tabs current={tab} onChange={setTab} />

          {tab === "browse" ? (
            <div className="flex min-h-[60vh] flex-col">
              <ModelLibraryBrowser
                mode="page"
                showHeader
                showSourceSidebar
                defaultAgeSegment="all"
              />
            </div>
          ) : null}

          {tab === "jobs" ? <ModelLibraryJobsPanel /> : null}

          {tab === "create" ? (
            <ModelLibraryGenerator
              onSubmit={handleGenerate}
              generating={generate.isPending}
            />
          ) : null}
        </div>
      </main>
      <ProjectMobileTabBar />
    </div>
  );
}

function Hero() {
  return (
    <section className="hidden md:block">
      <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        N°02 — Model Library
      </p>
      <h1 className="mt-3 font-display text-[44px] italic leading-[0.95] tracking-tight text-[var(--fg-0)] md:text-[64px]">
        模特库
      </h1>
      <p className="mt-4 max-w-xl text-[14px] leading-[1.7] text-[var(--fg-1)]">
        浏览预设、收藏、上传与生成的模特，集中管理你的全部模特资源。
      </p>
    </section>
  );
}

function Tabs({
  current,
  onChange,
}: {
  current: LibraryTab;
  onChange: (next: LibraryTab) => void;
}) {
  return (
    <div
      className={cn(
        "scrollbar-none -mx-4 flex gap-1 overflow-x-auto px-4 md:mx-0 md:overflow-visible md:px-0",
        "sticky top-0 z-10 bg-[var(--bg-0)]/85 backdrop-blur-xl md:relative md:top-auto md:z-auto md:bg-transparent md:backdrop-blur-none",
        "border-y border-[var(--border)]",
      )}
    >
      {TABS.map((option) => {
        const active = current === option.key;
        return (
          <button
            key={option.key}
            type="button"
            onClick={() => onChange(option.key)}
            aria-pressed={active}
            className={cn(
              "group relative inline-flex min-h-11 shrink-0 cursor-pointer items-center gap-2 px-4 py-3 font-mono text-[11px] uppercase tracking-[0.16em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:min-h-10 md:py-2.5",
              active
                ? "text-[var(--fg-0)]"
                : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
            )}
          >
            <span aria-hidden className="opacity-60">
              {option.eyebrow}
            </span>
            <span className="text-[var(--fg-0)]/80">·</span>
            <span>{option.label}</span>
            {active ? (
              <motion.span
                layoutId="library-tab-underline"
                aria-hidden
                className="absolute inset-x-3 -bottom-px h-px bg-[var(--amber-400)]"
                transition={SPRING.snap}
              />
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
