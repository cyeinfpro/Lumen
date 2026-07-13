"use client";

import {
  ArrowLeft,
  ArrowRight,
  Clapperboard,
  Image as ImageIcon,
  Shirt,
  Workflow,
} from "lucide-react";
import Link from "next/link";

import { useUiStore } from "@/store/useUiStore";
import {
  ProjectMobileTabBar,
  ProjectMobileTopBar,
  ProjectTopBar,
} from "@/components/ui/projects/components/ProjectTopBar";

export default function NewProjectPage() {
  const canvasEnabled = useUiStore((state) => state.canvasEnabled);
  const workflows = [
    {
      title: "无限画布",
      description: "自由连接提示词、素材、图片生成、视频生成与交付。",
      detail: "搭图 → 调参 → 运行 → 交付",
      href: "/projects/canvas/new",
      icon: Workflow,
      featureFlag: "canvas",
    },
    {
      title: "服饰模特图",
      description: "上传商品图，选择模特并生成可交付展示图。",
      detail: "商品图 → 模特 → 生成 → 质检",
      href: "/projects/apparel-model-showcase/new",
      icon: Shirt,
    },
    {
      title: "海报制作",
      description: "从素材、风格和文案生成多尺寸营销海报。",
      detail: "素材 → 风格 → 母版 → 导出",
      href: "/projects/poster-design/new",
      icon: ImageIcon,
    },
    {
      title: "分镜制作",
      description: "把想法推进到脚本、设定、镜头、视频和成片。",
      detail: "想法 → 脚本 → 分镜 → 成片",
      href: "/projects/storyboard?new=1",
      icon: Clapperboard,
    },
  ].filter(
    (workflow) =>
      !("featureFlag" in workflow) ||
      workflow.featureFlag !== "canvas" ||
      canvasEnabled,
  );

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <ProjectMobileTopBar
        title="新建项目"
        subtitle="选择工作流"
        backHref="/projects"
      />
      <ProjectTopBar />
      <main className="mb-[var(--mobile-tabbar-height)] min-h-0 flex-1 overflow-y-auto px-3 pb-8 pt-2 min-[390px]:px-4 md:mb-0 md:px-6 md:py-8">
        <div className="mx-auto w-full max-w-[960px]">
          <Link
            href="/projects"
            className="hidden min-h-[44px] items-center gap-2 text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] md:inline-flex"
          >
            <ArrowLeft className="h-4 w-4" />
            返回项目
          </Link>
          <header className="border-b border-[var(--border)] pb-5 pt-3">
            <p className="type-page-kicker">新建项目</p>
            <h1 className="type-page-title mt-2">选择工作流</h1>
            <p className="type-body-sm mt-2 max-w-xl text-[var(--fg-1)]">
              先选择交付目标。进入工作流后再补充素材和生成参数。
            </p>
          </header>
          <div className="grid gap-3 pt-4 md:grid-cols-2 xl:grid-cols-4">
            {workflows.map((workflow, index) => {
              const Icon = workflow.icon;
              return (
                <Link
                  key={workflow.title}
                  href={workflow.href}
                  className="surface-card surface-card-hover group grid min-h-[168px] content-between gap-5 rounded-[var(--radius-card)] p-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
                >
                  <div className="min-w-0">
                    <div className="flex items-start justify-between gap-3">
                      <span className="grid h-11 w-11 shrink-0 place-items-center rounded-[var(--radius-control)] bg-[var(--bg-2)] text-[var(--accent)] md:h-10 md:w-10">
                        <Icon className="h-5 w-5" />
                      </span>
                      <span className="type-mono-meta text-[var(--fg-3)]">
                        {String(index + 1).padStart(2, "0")}
                      </span>
                    </div>
                    <h2 className="type-card-title mt-4 break-words">{workflow.title}</h2>
                    <p className="type-body-sm mt-1 break-words text-[var(--fg-1)]">
                      {workflow.description}
                    </p>
                  </div>
                  <div className="flex min-h-[44px] items-center justify-between gap-3 border-t border-[var(--border-subtle)] pt-3">
                    <span className="type-caption min-w-0 break-words text-[var(--fg-2)]">
                      {workflow.detail}
                    </span>
                    <ArrowRight className="h-4 w-4 shrink-0 text-[var(--fg-2)] transition-transform group-hover:translate-x-0.5 group-hover:text-[var(--accent)]" />
                  </div>
                </Link>
              );
            })}
          </div>
        </div>
      </main>
      <ProjectMobileTabBar />
    </div>
  );
}
