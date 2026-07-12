"use client";

// 海报风格库主壳：风格库 / 任务中心 / 新建风格 三 tab。
// 与 ModelLibraryPage 的差异：
// - 没有"返回项目"按钮（风格库是独立功能，不嵌套在 /projects 下）
// - 顶部 backLabel="返回首页"

import { motion } from "framer-motion";
import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { toast } from "@/components/ui/primitives/Toast";
import { SPRING } from "@/lib/motion";
import { useGeneratePosterStyleMutation } from "@/lib/queries";
import type { PosterStyleGenerateIn } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import {
  ProjectMobileTabBar,
  ProjectMobileTopBar,
  ProjectTopBar,
} from "../projects/components/ProjectTopBar";
import { OnlineBanner } from "../projects/components/OnlineBanner";
import { PosterStyleBrowser } from "./PosterStyleBrowser";
import { PosterStyleGenerator } from "./PosterStyleGenerator";
import { PosterStyleJobsPanel } from "./PosterStyleJobsPanel";

type LibraryTab = "browse" | "jobs" | "create";

const TABS: Array<{ key: LibraryTab; label: string }> = [
  { key: "browse", label: "风格库" },
  { key: "jobs", label: "任务中心" },
  { key: "create", label: "新建风格" },
];

export function PosterStylePage() {
  const searchParams = useSearchParams();
  const initialTab = parseLibraryTab(searchParams.get("tab"));
  const [tab, setTab] = useState<LibraryTab>(initialTab);

  const generate = useGeneratePosterStyleMutation({
    onSuccess: () => {
      toast.success("已派发风格生成任务", {
        description: "已切到任务中心，可在这里查看进度",
      });
      setTab("jobs");
    },
    onError: (err) =>
      toast.error("派发失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });

  const handleGenerate = async (body: PosterStyleGenerateIn) => {
    await generate.mutateAsync(body);
  };

  // 从 jobs 面板点击"查看入库"时，切到 browse；详情抽屉自身在 PosterStyleBrowser
  // 里以 itemId state 打开。这里通过 sessionStorage 桥接，避免 props drilling。
  const handleOpenItemFromJob = (itemId: string) => {
    setTab("browse");
    if (typeof window !== "undefined") {
      try {
        window.sessionStorage.setItem("posterStyle.openItemId", itemId);
      } catch {
        // ignore
      }
    }
  };

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="风格库"
        backHref="/"
        backLabel="返回首页"
      />
      <ProjectTopBar />

      <main className="lumen-studio-bg project-mobile-scroll mb-[calc(var(--mobile-tabbar-h)+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-[max(0.75rem,env(safe-area-inset-left,0px))] pb-[calc(1rem+env(safe-area-inset-bottom,0px))] pt-1 md:mb-0 md:px-6 md:pb-6 md:pt-3">
        <div className="mx-auto grid w-full max-w-[1520px] gap-3">
          <LibraryHeader current={tab} onChange={setTab} />

          <Tabs
            current={tab}
            onChange={setTab}
            className="sticky top-0 z-20 -mx-3 overflow-x-auto border-b bg-[var(--bg-0)]/95 px-3 shadow-[var(--shadow-1)] backdrop-blur-xl [scrollbar-width:none] md:hidden"
            compact
          />

          {tab === "browse" ? (
            <div className="flex min-h-[56vh] flex-col">
              <PosterStyleBrowser onCreateClick={() => setTab("create")} />
            </div>
          ) : null}

          {tab === "jobs" ? (
            <PosterStyleJobsPanel onOpenItem={handleOpenItemFromJob} />
          ) : null}

          {tab === "create" ? (
            <PosterStyleGenerator
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
        <p className="type-page-kicker shrink-0">风格库</p>
        <h1 className="type-page-title shrink-0">风格库</h1>
        <p className="type-page-subtitle hidden min-w-0 truncate lg:block">
          预设、收藏、上传与生成的海报风格集中管理。
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Tabs current={current} onChange={onChange} compact />
        <Link
          href="/"
          className="inline-flex min-h-9 items-center gap-1.5 border border-[var(--border)] px-3 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          返回首页
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
                ? "min-h-11 px-3 py-1.5 text-[12px] md:min-h-9"
                : "min-h-10 px-3 py-2.5 text-[13px] md:min-h-9 md:px-3 md:py-2",
              active
                ? "text-[var(--fg-0)]"
                : "text-[var(--fg-2)] hover:text-[var(--fg-1)]",
            )}
          >
            {option.label}
            {active ? (
              <motion.span
                layoutId="poster-style-tab-underline"
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
