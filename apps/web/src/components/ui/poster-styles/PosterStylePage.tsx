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
import type { PosterStyleGenerateIn } from "@/lib/apiClient";
import {
  useGeneratePosterStyleMutation,
  usePosterStyleJobsQuery,
} from "@/lib/queries";
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
  const jobs = usePosterStyleJobsQuery({ limit: 50 });

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
  // 里消费并清理一次性 itemId。先写入再切 tab，确保浏览器挂载时可读取。
  const handleOpenItemFromJob = (itemId: string) => {
    if (typeof window !== "undefined") {
      try {
        window.sessionStorage.setItem("posterStyle.openItemId", itemId);
      } catch {
        // ignore
      }
    }
    setTab("browse");
  };

  return (
    <div className="page-shell relative h-[100dvh]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="风格库"
        backHref="/"
        backLabel="返回首页"
      />
      <ProjectTopBar />

      <main className="page-scroll lumen-studio-bg project-mobile-scroll mb-[var(--mobile-tabbar-height)]">
        <div className="page-frame grid max-w-[1520px] gap-3">
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
            <PosterStyleJobsPanel jobs={jobs} onOpenItem={handleOpenItemFromJob} />
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
    <header className="page-header hidden md:grid">
      <div className="page-header-copy">
        <p className="type-page-kicker">风格库</p>
        <h1 className="type-page-title">风格库</h1>
        <p className="type-page-subtitle hidden max-w-3xl lg:block">
          预设、收藏、上传与生成的海报风格集中管理。
        </p>
      </div>
      <div className="page-header-actions">
        <Tabs current={current} onChange={onChange} compact />
        <Link
          href="/"
          className="inline-flex min-h-9 items-center gap-1.5 border border-[var(--border)] px-3 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          返回首页
        </Link>
      </div>
    </header>
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
        compact ? "" : "border-b border-[var(--border)]",
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
