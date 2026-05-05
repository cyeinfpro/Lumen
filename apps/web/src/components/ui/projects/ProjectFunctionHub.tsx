"use client";

// 项目功能中心：与「创作 / 图库 / 我的」对齐的克制版式。
// - 页面信息带 + 紧凑描述，避免营销式 hero
// - Feature 卡：工作流缩略区 + 清晰标题 + 描述
// - 不可用卡：低对比度 + Soon label

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

      <main className="lumen-studio-bg mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-12 pt-3 md:mb-0 md:px-10 md:py-8">
        <div className="mx-auto grid w-full max-w-[1440px] gap-6 md:gap-8">
          <header className="hidden md:block">
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
              Project Hub
            </p>
            <h1 className="mt-2 font-display text-[34px] italic leading-[1] text-[var(--fg-0)] md:text-[42px]">
              项目
            </h1>
            <p className="mt-3 max-w-xl text-[13px] leading-[1.7] text-[var(--fg-2)]">
              选择要创建和管理的项目类型。每一类工作流由 AI 串联多步生成、审稿与交付。
            </p>
          </header>

          <section className="grid gap-x-5 gap-y-7 sm:grid-cols-2 md:grid-cols-3 md:gap-x-6 md:gap-y-8">
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
          "relative h-28 overflow-hidden rounded-lg border border-[var(--border-subtle)] bg-[var(--bg-1)] transition-all duration-[var(--dur-base)] sm:h-36 md:h-44",
          feature.available
            ? "group-hover:border-[var(--border-amber)]/60"
            : "opacity-60",
        )}
      >
        <div className="absolute inset-0 flex items-center justify-center">
          <Icon
            className={cn(
              "h-10 w-10 transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] sm:h-14 sm:w-14",
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
            className="absolute right-3 top-3 inline-flex h-8 w-8 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--bg-0)]/40 backdrop-blur transition-all duration-[var(--dur-base)] group-hover:border-[var(--border-amber)] group-hover:bg-[var(--accent)] group-hover:text-black"
          >
            <ArrowUpRight className="h-3.5 w-3.5" />
          </span>
        ) : (
          <span className="absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--bg-0)]/40 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)] backdrop-blur">
            Soon
          </span>
        )}
      </div>
      <div className="mt-3">
        <div className="flex items-baseline gap-2">
          <h2
            className={cn(
              "text-[17px] font-semibold tracking-tight leading-[1.25] transition-colors duration-[var(--dur-base)] sm:text-[18px] md:text-[19px]",
              feature.available
                ? "text-[var(--fg-0)] group-hover:text-[var(--amber-300)]"
                : "text-[var(--fg-2)]",
            )}
          >
            {feature.title}
          </h2>
          <span
            className={cn(
              "font-mono text-[10px] uppercase tracking-[0.18em]",
              feature.available ? "text-[var(--fg-2)]" : "text-[var(--fg-3)]",
            )}
          >
            {feature.en}
          </span>
        </div>
        <p
          className={cn(
            "mt-1.5 max-w-md text-[12.5px] leading-[1.6]",
            feature.available ? "text-[var(--fg-2)]" : "text-[var(--fg-3)]",
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
