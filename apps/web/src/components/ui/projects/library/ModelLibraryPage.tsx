"use client";

// 与「创作 / 图库 / 我的」对齐的克制版式：页面信息带 + hairline tab。
// /library 主壳：模特库 / 任务中心 / 新建模特 三 tab。
// 提交"新建模特"后自动切到"任务中心"。

import { motion } from "framer-motion";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
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
  const searchParams = useSearchParams();
  const initialTab = parseLibraryTab(searchParams.get("tab"));
  const [tab, setTab] = useState<LibraryTab>(initialTab);
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
        backHref="/projects"
        backLabel="返回项目"
      />
      <ProjectTopBar />

      <main className="lumen-studio-bg project-mobile-scroll mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 pt-1 md:mb-0 md:px-6 md:pb-6 md:pt-3">
        <div className="mx-auto grid w-full max-w-[1520px] gap-3">
          <LibraryHeader current={tab} onChange={setTab} />

          <Tabs current={tab} onChange={setTab} className="md:hidden" compact />

          {tab === "browse" ? (
            <div className="flex min-h-[56vh] flex-col">
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

function parseLibraryTab(value: string | null): LibraryTab {
  if (value === "jobs" || value === "create" || value === "browse") return value;
  return "browse";
}

function LibraryHeader({
  current,
  onChange,
}: {
  current: LibraryTab;
  onChange: (next: LibraryTab) => void;
}) {
  return (
    <section className="hidden min-w-0 items-center justify-between gap-3 border-b border-[var(--border)] pb-1.5 md:flex">
      <div className="flex min-w-0 items-baseline gap-2.5">
        <p className="shrink-0 font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          模特库
        </p>
        <h1 className="shrink-0 font-display text-[24px] italic leading-[1] text-[var(--fg-0)]">
          模特库
        </h1>
        <p className="hidden min-w-0 truncate text-[12px] leading-5 text-[var(--fg-2)] lg:block">
          浏览预设、收藏、上传与生成的模特，集中管理你的全部模特资源。
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Tabs current={current} onChange={onChange} compact />
        <Link
          href="/projects"
          className="inline-flex h-7 items-center gap-1.5 border border-[var(--border)] px-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          返回项目
        </Link>
      </div>
    </section>
  );
}

function Tabs({
  current,
  onChange,
  className,
  compact = false,
}: {
  current: LibraryTab;
  onChange: (next: LibraryTab) => void;
  className?: string;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex min-w-0 flex-wrap gap-x-1 gap-y-1",
        compact
          ? ""
          : "sticky top-0 z-10 bg-[var(--bg-0)]/85 backdrop-blur-xl md:relative md:top-auto md:z-auto md:bg-transparent md:backdrop-blur-none",
        "border-b border-[var(--border)]",
        className,
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
              "group relative inline-flex shrink-0 cursor-pointer items-center font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
              compact
                ? "min-h-7 px-2.5 py-1 text-[12px]"
                : "min-h-10 px-3 py-2.5 text-[13px] md:min-h-9 md:px-3 md:py-2",
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
