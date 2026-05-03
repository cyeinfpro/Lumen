"use client";

import Link from "next/link";
import { ArrowLeft, ChevronRight, PackageCheck, Shirt, Sparkles } from "lucide-react";

import { ProjectMobileTabBar, ProjectMobileTopBar, ProjectTopBar } from "@/components/ui/projects/components/ProjectTopBar";

export default function NewProjectPage() {
  return (
    <div className="relative flex h-[100dvh] w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <ProjectMobileTopBar title="新建" subtitle="选择项目模板" backHref="/projects" />
      <ProjectTopBar />

      <main className="mb-[calc(56px+env(safe-area-inset-bottom,0px))] flex-1 overflow-y-auto overscroll-contain px-3 pb-4 pt-3 md:mb-0 md:px-8 md:py-6">
        <div className="mx-auto max-w-5xl">
          <nav aria-label="项目路径" className="hidden items-center gap-1.5 text-sm md:flex">
            <Link
              href="/projects"
              className="inline-flex items-center gap-1.5 text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)]"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              项目
            </Link>
            <span aria-hidden className="text-[var(--fg-3)]">/</span>
            <span className="text-[var(--fg-0)]">模板</span>
          </nav>
          <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/70 p-4 shadow-[var(--shadow-1)] md:mt-0 md:rounded-none md:border-0 md:bg-transparent md:p-0 md:shadow-none">
            <h1 className="text-[26px] font-semibold tracking-normal md:mt-1 md:text-[32px]">
              新建项目
            </h1>
            <p className="mt-1 text-sm leading-6 text-[var(--fg-2)]">
              选择一个可复用工作流，后续项目会沿用同一套导航、筛选和交付节奏。
            </p>
          </div>

          <div className="mt-4 grid gap-3 md:mt-5 md:grid-cols-3">
            <TemplateLink
              href="/projects/apparel-model-showcase/new"
              title="服饰模特展示图"
              description="商品理解、模特候选、确认模特、展示图生成、质检返修。"
              active
              icon={<Shirt className="h-5 w-5" />}
            />
            <TemplateLink
              title="商品图精修"
              description="保留商品主体，做清洁、校色和基础电商化处理。"
              icon={<PackageCheck className="h-5 w-5" />}
            />
            <TemplateLink
              title="批量商品上新"
              description="为同一系列商品复用风格配置和输出规格。"
              icon={<Sparkles className="h-5 w-5" />}
            />
          </div>
        </div>
      </main>
      <ProjectMobileTabBar />
    </div>
  );
}

function TemplateLink({
  title,
  description,
  icon,
  href,
  active = false,
}: {
  title: string;
  description: string;
  icon: React.ReactNode;
  href?: string;
  active?: boolean;
}) {
  const content = (
    <>
      <div className="flex items-start justify-between gap-3">
        <span className="flex h-10 w-10 items-center justify-center rounded-md bg-[var(--accent-soft)] text-[var(--amber-300)]">
          {icon}
        </span>
        {active ? (
          <ChevronRight className="h-4 w-4 text-[var(--fg-2)]" />
        ) : (
          <span className="rounded-md border border-[var(--border)] px-2 py-1 text-[11px] text-[var(--fg-2)]">
            后续
          </span>
        )}
      </div>
      <h2 className="mt-4 text-base font-medium tracking-normal">{title}</h2>
      <p className="mt-2 text-sm leading-6 text-[var(--fg-2)]">{description}</p>
    </>
  );

  const className = [
    "min-h-44 rounded-xl border p-4 text-left transition-[background-color,border-color,opacity] md:rounded-md",
    active
      ? "cursor-pointer border-[var(--border-amber)] bg-[var(--accent-soft)] hover:bg-[var(--accent-soft)]/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
      : "border-[var(--border)] bg-white/[0.025] opacity-70",
  ].join(" ");

  if (!href) {
    return <div className={className}>{content}</div>;
  }
  return (
    <Link href={href} className={className}>
      {content}
    </Link>
  );
}
