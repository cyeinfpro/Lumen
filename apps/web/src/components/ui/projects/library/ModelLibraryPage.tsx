"use client";

// /projects/library 主壳：模特库 / 任务中心 / 新建模特 三 tab。
//
// 提交"新建模特"后自动切到"任务中心"，给用户即时反馈"任务已派发"。
// 顶部复用 ProjectTopBar / ProjectMobileTopBar，不破坏现有项目导航语言。

import { ArrowLeft, FolderKanban, Library, ListChecks, WandSparkles } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { toast } from "@/components/ui/primitives/Toast";
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

const TABS: Array<{ key: LibraryTab; label: string; icon: React.ReactNode }> = [
  { key: "browse", label: "模特库", icon: <Library className="h-3.5 w-3.5" /> },
  { key: "jobs", label: "任务中心", icon: <ListChecks className="h-3.5 w-3.5" /> },
  { key: "create", label: "新建模特", icon: <WandSparkles className="h-3.5 w-3.5" /> },
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
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="模特库"
        subtitle="浏览 / 任务 / 新建"
        backHref="/projects"
      />
      <ProjectTopBar />

      <main className="mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 pb-4 pt-3 md:mb-0 md:px-8 md:py-5">
        <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 md:gap-5">
          <nav
            aria-label="模特库路径"
            className="hidden items-center gap-1.5 text-sm md:flex"
          >
            <Link
              href="/projects"
              className="inline-flex items-center gap-1.5 text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)]"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              项目
            </Link>
            <span aria-hidden className="text-[var(--fg-3)]">
              /
            </span>
            <span className="text-[var(--fg-0)]">模特库</span>
          </nav>

          <Hero />

          <Tabs current={tab} onChange={setTab} />

          {tab === "browse" ? (
            <div className="flex min-h-[60vh] flex-col overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/60 shadow-[var(--shadow-1)] md:rounded-md md:bg-white/[0.025]">
              <ModelLibraryBrowser
                mode="page"
                showHeader={false}
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
    <section className="grid gap-3 rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/78 p-4 shadow-[var(--shadow-1)] md:grid-cols-[minmax(0,1fr)_auto] md:items-end md:rounded-md md:bg-white/[0.035] md:p-5">
      <div className="min-w-0">
        <p className="flex items-center gap-2 text-[11px] font-medium tracking-[0.16em] text-[var(--fg-2)]">
          <Library className="h-3.5 w-3.5" />
          MODEL LIBRARY
        </p>
        <div className="mt-2 flex flex-wrap items-end gap-x-3 gap-y-1">
          <h1 className="text-[28px] font-semibold tracking-normal text-[var(--fg-0)] md:text-[34px]">
            模特库
          </h1>
        </div>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
          浏览全站预设、我的收藏和上传的模特图。也可以独立生成一批模特，再挑喜欢的入库——和项目里的模特候选共享一个任务中心。
        </p>
      </div>
      <Link
        href="/projects"
        className="inline-flex min-h-11 items-center justify-center gap-2 rounded-md border border-[var(--border)] bg-white/[0.04] px-4 text-[15px] font-medium text-[var(--fg-0)] transition-colors hover:border-[var(--border-strong)] hover:bg-white/[0.08] md:h-10 md:min-h-0 md:text-sm"
      >
        <FolderKanban className="h-4 w-4" />
        返回项目
      </Link>
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
    <div className="scrollbar-none -mx-1 flex gap-1.5 overflow-x-auto px-1 pb-0.5 md:flex-wrap md:overflow-visible md:pb-0">
      {TABS.map((option) => {
        const active = current === option.key;
        return (
          <button
            key={option.key}
            type="button"
            onClick={() => onChange(option.key)}
            className={cn(
              "inline-flex min-h-10 shrink-0 cursor-pointer items-center gap-1.5 rounded-full border px-3 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:h-9 md:min-h-0",
              active
                ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/[0.04]",
            )}
            aria-pressed={active}
          >
            {option.icon}
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
