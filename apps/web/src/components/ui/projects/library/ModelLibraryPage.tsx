"use client";

// /library 主壳：模特库 / 任务中心 / 新建模特 三 tab。已从 /projects/library 提到顶层。
//
// 提交"新建模特"后自动切到"任务中心"，给用户即时反馈"任务已派发"。
// 顶部复用 ProjectTopBar / ProjectMobileTopBar，不破坏现有项目导航语言。

import { motion } from "framer-motion";
import { Library, ListChecks, WandSparkles } from "lucide-react";
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
      />
      <ProjectTopBar />

      <main className="mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 pb-4 pt-3 md:mb-0 md:px-8 md:py-5">
        <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-5 md:gap-6">
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
  // 移动端 ProjectMobileTopBar 已经显示标题，hero 整块隐藏；桌面双列：左标题 + 右装饰圆
  return (
    <section className="hidden rounded-2xl border border-[var(--border)] bg-[var(--bg-1)] p-6 shadow-[var(--shadow-1)] md:grid md:grid-cols-[minmax(0,1fr)_auto] md:items-center md:gap-3 md:p-8">
      <div className="min-w-0">
        <p className="flex items-center gap-2 font-mono text-[11px] font-medium tracking-[0.16em] text-[var(--fg-2)]">
          <Library className="h-3.5 w-3.5" />
          MODEL LIBRARY
        </p>
        <h1 className="mt-3 font-display text-[40px] italic leading-tight text-[var(--fg-0)]">
          模特库
        </h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
          浏览预设、收藏、上传与生成的模特，集中管理你的全部模特资源。
        </p>
      </div>
      <div
        aria-hidden
        className="hidden h-24 w-24 shrink-0 rounded-full bg-gradient-to-tr from-[var(--amber-400)]/40 to-[var(--amber-200)]/10 shadow-[var(--shadow-amber)] md:block"
      />
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
  // 移动端：sticky 胶囊；桌面端：底部 layoutId 下划线（更接近主导航语言）
  return (
    <div
      className={cn(
        "scrollbar-none -mx-1 flex gap-1.5 overflow-x-auto px-1 pb-0.5",
        "sticky top-0 z-10 bg-[var(--bg-0)]/85 backdrop-blur-xl",
        "md:relative md:top-auto md:z-auto md:gap-1.5 md:overflow-visible md:bg-transparent md:px-0 md:pb-0 md:backdrop-blur-none",
        "md:border-b md:border-[var(--border)]",
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
              "relative inline-flex shrink-0 cursor-pointer items-center gap-1.5 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
              // 移动端：胶囊
              "min-h-10 rounded-full border px-3 text-xs",
              // 桌面端：纯文本 + 底部下划线
              "md:min-h-0 md:rounded-none md:border-0 md:px-4 md:py-2.5 md:text-sm",
              active
                ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)] md:border-0 md:bg-transparent md:text-[var(--fg-0)]"
                : "border-[var(--border)] text-[var(--fg-1)] hover:bg-white/[0.04] md:border-0 md:bg-transparent md:text-[var(--fg-2)] md:hover:bg-transparent md:hover:text-[var(--fg-0)]",
            )}
          >
            {option.icon}
            {option.label}
            {active ? (
              <motion.span
                layoutId="library-tab-underline"
                aria-hidden
                className="absolute -bottom-px left-2 right-2 hidden h-0.5 rounded-full bg-[var(--amber-400)] shadow-[var(--shadow-amber)] md:block"
                transition={SPRING.snap}
              />
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
