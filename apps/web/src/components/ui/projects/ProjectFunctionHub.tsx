"use client";

import { ChevronRight, Film, Image as ImageIcon, Shirt } from "lucide-react";
import Link from "next/link";

import { cn } from "@/lib/utils";
import { OnlineBanner } from "./components/OnlineBanner";
import { ProjectMobileTabBar, ProjectMobileTopBar, ProjectTopBar } from "./components/ProjectTopBar";

const FEATURES = [
  {
    title: "服饰模特图",
    description: "上传商品图，管理模特库、候选模特、展示图生成和交付流程。",
    href: "/projects/apparel-model-showcase",
    icon: Shirt,
    available: true,
  },
  {
    title: "海报制作",
    description: "为商品、活动和品牌场景生成海报版式。（后续）",
    icon: ImageIcon,
    available: false,
  },
  {
    title: "分镜制作",
    description: "将商品卖点拆成镜头脚本和画面分镜。（后续）",
    icon: Film,
    available: false,
  },
] as const;

export function ProjectFunctionHub() {
  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar title="项目" subtitle="选择要创建和管理的项目类型" />
      <ProjectTopBar />

      <main className="mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 pb-4 pt-3 md:mb-0 md:px-8 md:py-5">
        <div className="mx-auto grid w-full max-w-[1240px] gap-4 md:gap-5">
          <div className="grid gap-1">
            <p className="font-mono text-[11px] tracking-[0.16em] text-[var(--fg-2)]">
              PROJECT HUB
            </p>
            <h1 className="text-[24px] font-medium tracking-normal text-[var(--fg-0)] md:text-[28px]">
              项目
            </h1>
            <p className="max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
              选择要创建和管理的项目类型。
            </p>
          </div>

          <section className="grid gap-3 lg:grid-cols-3">
            <FeatureCard feature={FEATURES[0]} className="lg:col-span-1" />
            <FeatureCard feature={FEATURES[1]} />
            <FeatureCard feature={FEATURES[2]} />
          </section>
        </div>
      </main>

      <ProjectMobileTabBar />
    </div>
  );
}

function FeatureCard({
  feature,
  className,
}: {
  feature: (typeof FEATURES)[number];
  className?: string;
}) {
  const Icon = feature.icon;
  const content = (
    <>
      <div className="flex items-start justify-between gap-3">
        <span
          className={cn(
            "flex h-10 w-10 items-center justify-center rounded-md border",
            feature.available
              ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
              : "border-[var(--border)] bg-white/[0.04] text-[var(--fg-2)]",
          )}
        >
          <Icon className="h-5 w-5" />
        </span>
        {feature.available ? (
          <ChevronRight className="h-4 w-4 text-[var(--fg-2)] transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-[var(--fg-0)]" />
        ) : (
          <span className="rounded-md border border-[var(--border)] px-2 py-1 text-[11px] text-[var(--fg-2)]">
            后续
          </span>
        )}
      </div>
      <h2 className="mt-4 text-base font-medium tracking-normal text-[var(--fg-0)]">
        {feature.title}
      </h2>
      <p className="mt-2 max-w-md text-sm leading-6 text-[var(--fg-2)]">
        {feature.description}
      </p>
    </>
  );

  const classes = cn(
    "min-h-40 rounded-xl border p-4 text-left transition-[background-color,border-color,opacity] md:rounded-md",
    feature.available
      ? "group cursor-pointer border-[var(--border-amber)]/60 bg-[var(--accent-soft)] hover:bg-[var(--accent-soft)]/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
      : "border-[var(--border)] bg-white/[0.028] opacity-70",
    className,
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
