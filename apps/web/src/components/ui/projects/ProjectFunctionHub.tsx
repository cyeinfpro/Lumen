"use client";

// 项目功能中心：editorial hub 入口。
// - Hero 大字 + mono eyebrow
// - Feature 卡：portrait 上图占位 + serif 大标题 + N° 序号
// - 不可用卡：低对比度 + Coming label，不再 amber-soft

import { ArrowUpRight, Film, Image as ImageIcon, Shirt } from "lucide-react";
import Link from "next/link";

import { cn } from "@/lib/utils";
import { OnlineBanner } from "./components/OnlineBanner";
import { ProjectMobileTabBar, ProjectMobileTopBar, ProjectTopBar } from "./components/ProjectTopBar";

const FEATURES = [
  {
    title: "服饰模特图",
    en: "Apparel Studio",
    description: "上传商品图，管理模特库、候选模特、展示图生成与交付流程。",
    href: "/projects/apparel-model-showcase",
    icon: Shirt,
    available: true,
  },
  {
    title: "海报制作",
    en: "Poster",
    description: "为商品、活动和品牌场景生成海报版式。",
    icon: ImageIcon,
    available: false,
  },
  {
    title: "分镜制作",
    en: "Storyboard",
    description: "将商品卖点拆成镜头脚本与画面分镜。",
    icon: Film,
    available: false,
  },
] as const;

export function ProjectFunctionHub() {
  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar title="项目" subtitle="PROJECT HUB" />
      <ProjectTopBar />

      <main className="lumen-studio-bg mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-12 pt-3 md:mb-0 md:px-10 md:py-6">
        <div className="mx-auto grid w-full max-w-[1280px] gap-5 md:gap-14">
          <header className="hidden gap-3 md:grid">
            <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              N°00 — Project Hub
            </p>
            <h1 className="font-display text-[40px] italic leading-[0.95] tracking-tight text-[var(--fg-0)] sm:text-[44px] md:text-[72px]">
              项目
            </h1>
            <p className="max-w-xl text-[14px] leading-7 text-[var(--fg-1)]">
              选择要创建和管理的项目类型。每一类工作流由 AI 串联多步生成、审稿与交付。
            </p>
          </header>

          <section className="grid gap-x-6 gap-y-6 sm:grid-cols-2 md:grid-cols-3 md:gap-y-10">
            {FEATURES.map((feature, index) => (
              <FeatureCard key={feature.title} feature={feature} index={index} />
            ))}
          </section>
        </div>
      </main>

      <ProjectMobileTabBar />
    </div>
  );
}

function FeatureCard({
  feature,
  index,
}: {
  feature: (typeof FEATURES)[number];
  index: number;
}) {
  const Icon = feature.icon;
  const num = `N°${String(index + 1).padStart(2, "0")}`;
  const content = (
    <>
      <div
        className={cn(
          "relative h-28 overflow-hidden border border-[var(--border)] bg-[var(--bg-1)] transition-all duration-[var(--dur-base)] sm:h-40 md:h-52",
          feature.available
            ? "group-hover:border-[var(--border-amber)]/60"
            : "opacity-60",
        )}
      >
        <div className="absolute inset-0 flex items-center justify-center">
          <Icon
            className={cn(
              "h-12 w-12 transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] sm:h-20 sm:w-20",
              feature.available
                ? "text-[var(--fg-1)] group-hover:scale-[1.08] group-hover:text-[var(--amber-300)]"
                : "text-[var(--fg-3)]",
            )}
            strokeWidth={1.25}
          />
        </div>
        <span className="absolute left-3 top-3 font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-2)]">
          {num}
        </span>
        {feature.available ? (
          <span
            aria-hidden
            className="absolute right-3 top-3 inline-flex h-9 w-9 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--bg-0)]/40 backdrop-blur transition-all duration-[var(--dur-base)] group-hover:border-[var(--border-amber)] group-hover:bg-[var(--accent)] group-hover:text-black"
          >
            <ArrowUpRight className="h-4 w-4" />
          </span>
        ) : (
          <span className="absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--bg-0)]/40 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)] backdrop-blur">
            Soon
          </span>
        )}
      </div>
      <div className="mt-3 sm:mt-4">
        <p
          className={cn(
            "font-mono text-[10px] uppercase tracking-[0.22em]",
            feature.available ? "text-[var(--amber-300)]" : "text-[var(--fg-3)]",
          )}
        >
          {feature.en}
        </p>
        <h2
          className={cn(
            "mt-1.5 font-display text-[24px] italic leading-[1.1] transition-colors duration-[var(--dur-base)] sm:text-[26px] md:text-[30px]",
            feature.available
              ? "text-[var(--fg-0)] group-hover:text-[var(--amber-300)]"
              : "text-[var(--fg-2)]",
          )}
        >
          {feature.title}
        </h2>
        <p
          className={cn(
            "mt-2 max-w-md text-[13px] leading-6",
            feature.available ? "text-[var(--fg-1)]" : "text-[var(--fg-2)]",
          )}
        >
          {feature.description}
        </p>
      </div>
    </>
  );

  const classes = cn(
    "block focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
    feature.available ? "group cursor-pointer" : "cursor-default",
  );

  if (feature.available && feature.href) {
    return (
      <Link href={feature.href} className={classes}>
        {content}
      </Link>
    );
  }

  return (
    <div className={classes} aria-disabled="true">
      {content}
    </div>
  );
}
