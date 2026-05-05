"use client";

// 与「创作 / 图库 / 我的」对齐的克制版式：页面信息带 + hairline tab。
// /library 主壳：模特库 / 任务中心 / 新建模特 三 tab。
// 提交"新建模特"后自动切到"任务中心"。

import { motion } from "framer-motion";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
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

const TABS: Array<{ key: LibraryTab; label: string }> = [
  { key: "browse", label: "模特库" },
  { key: "jobs", label: "任务中心" },
  { key: "create", label: "新建模特" },
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
        backHref="/projects"
        backLabel="返回项目"
      />
      <ProjectTopBar />

      <main className="lumen-studio-bg mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-12 pt-3 md:mb-0 md:px-10 md:py-8">
        <div className="mx-auto grid w-full max-w-[1440px] gap-6 md:gap-8">
          <Hero />

          <Tabs current={tab} onChange={setTab} />

          {tab === "browse" ? (
            <div className="flex min-h-[60vh] flex-col">
              <ModelLibraryBrowser
                mode="page"
                showHeader
                showSourceSidebar
                defaultAgeSegment="all"
                headerExtra={
                  <Link
                    href="/projects"
                    className="inline-flex h-9 items-center gap-2 border border-[var(--border)] px-3 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
                  >
                    <ArrowLeft className="h-3.5 w-3.5" />
                    返回项目
                  </Link>
                }
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
    <section className="hidden border-b border-[var(--border)] pb-5 md:block">
      <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        Library
      </p>
      <h1 className="mt-2 font-display text-[36px] italic leading-[1] text-[var(--fg-0)] md:text-[42px]">
        模特库
      </h1>
      <p className="mt-3 max-w-2xl text-[13px] leading-[1.7] text-[var(--fg-2)]">
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
        "flex min-w-0 flex-wrap gap-x-1 gap-y-1",
        "sticky top-0 z-10 bg-[var(--bg-0)]/85 backdrop-blur-xl md:relative md:top-auto md:z-auto md:bg-transparent md:backdrop-blur-none",
        "border-b border-[var(--border)]",
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
              "group relative inline-flex min-h-11 shrink-0 cursor-pointer items-center px-3 py-3 text-[13px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 md:min-h-10 md:px-4 md:py-2.5",
              active
                ? "text-[var(--fg-0)]"
                : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
            )}
          >
            {option.label}
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
