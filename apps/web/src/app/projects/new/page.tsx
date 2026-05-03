"use client";

import Link from "next/link";
import { ArrowLeft, ChevronRight, PackageCheck, Shirt, Sparkles } from "lucide-react";

export default function NewProjectPage() {
  return (
    <div className="flex min-h-[100dvh] flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <header className="sticky top-0 z-20 flex h-11 items-center justify-between border-b border-white/[0.05] bg-[var(--bg-0)]/80 px-3 backdrop-blur-xl md:px-5">
        <Link href="/projects" className="inline-flex items-center gap-2 text-sm text-[var(--fg-1)]">
          <ArrowLeft className="h-4 w-4" />
          项目
        </Link>
        <Link href="/" className="text-sm text-[var(--fg-2)] hover:text-[var(--fg-0)]">
          创作
        </Link>
      </header>

      <main className="flex-1 overflow-y-auto px-4 py-6 md:px-8">
        <div className="mx-auto max-w-5xl">
          <p className="text-xs text-[var(--fg-2)]">项目模板</p>
          <h1 className="mt-1 text-[26px] font-semibold tracking-normal md:text-[32px]">
            新建项目
          </h1>

          <div className="mt-5 grid gap-3 md:grid-cols-3">
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
    "min-h-44 rounded-md border p-4 text-left transition-colors",
    active
      ? "border-[var(--border-amber)] bg-[var(--accent-soft)] hover:bg-[var(--accent-soft)]/80"
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
