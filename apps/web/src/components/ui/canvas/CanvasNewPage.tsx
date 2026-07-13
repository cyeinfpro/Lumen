"use client";

import {
  ArrowLeft,
  Boxes,
  Film,
  Image as ImageIcon,
  LayoutGrid,
  PackageOpen,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useCreateCanvasMutation } from "@/lib/queries/canvases";
import { createCanvasTemplateGraph } from "@/lib/canvas/graph";
import { Button, Input, toast } from "@/components/ui/primitives";
import {
  ProjectMobileTabBar,
  ProjectMobileTopBar,
  ProjectTopBar,
} from "@/components/ui/projects/components/ProjectTopBar";

const TEMPLATES = [
  {
    key: "blank",
    title: "空白画布",
    description: "从默认提示词与图片生成开始。",
    detail: "提示词 → 图片生成",
    icon: Workflow,
  },
  {
    key: "image_to_video",
    title: "图片到视频",
    description: "先生成关键视觉，再连接视频节点。",
    detail: "提示词 → 图片 → 视频 → 交付",
    icon: Film,
  },
  {
    key: "product_directions",
    title: "商品图多方向",
    description: "一组商品素材分支多个视觉方向。",
    detail: "商品图 → 多分支图片",
    icon: PackageOpen,
  },
  {
    key: "multi_ratio",
    title: "多比例视觉",
    description: "同一提示词并行生成常用比例。",
    detail: "1:1 · 4:5 · 9:16 · 16:9",
    icon: LayoutGrid,
  },
  {
    key: "storyboard_video",
    title: "关键帧到视频",
    description: "组织关键帧并生成对应视频段。",
    detail: "画框 → 关键帧 → 视频",
    icon: Boxes,
  },
] as const;

export function CanvasNewPage() {
  const router = useRouter();
  const [title, setTitle] = useState("未命名画布");
  const [selected, setSelected] = useState<(typeof TEMPLATES)[number]["key"]>("blank");
  const create = useCreateCanvasMutation();
  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <ProjectMobileTopBar
        title="新建画布"
        subtitle="选择模板"
        backHref="/projects/canvas"
      />
      <ProjectTopBar />
      <main className="mb-[var(--mobile-tabbar-height)] min-h-0 flex-1 overflow-y-auto px-3 pb-8 pt-3 min-[390px]:px-4 md:mb-0 md:px-6 md:py-8">
        <div className="mx-auto w-full max-w-[1080px]">
          <Link
            href="/projects/canvas"
            className="hidden min-h-10 items-center gap-2 type-body-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)] md:inline-flex"
          >
            <ArrowLeft className="h-4 w-4" />
            返回画布
          </Link>
          <header className="border-b border-[var(--border)] pb-5 pt-2">
            <p className="type-page-kicker">New Canvas</p>
            <h1 className="type-page-title mt-1">创建无限画布</h1>
            <p className="type-body-sm mt-2 max-w-xl text-[var(--fg-1)]">
              模板只创建画布结构。生成任务会在进入工作区后由你明确运行。
            </p>
          </header>
          <div className="grid gap-6 pt-5 lg:grid-cols-[minmax(0,1fr)_280px]">
            <section>
              <h2 className="type-overline text-[var(--fg-2)]">模板</h2>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                {TEMPLATES.map((template) => {
                  const Icon = template.icon;
                  const active = selected === template.key;
                  return (
                    <button
                      key={template.key}
                      type="button"
                      aria-pressed={active}
                      onClick={() => setSelected(template.key)}
                      className={`min-h-[160px] rounded-[var(--radius-card)] border p-4 text-left transition-[border-color,background-color,box-shadow] ${
                        active
                          ? "border-[var(--accent)] bg-[var(--accent-soft)] shadow-[var(--shadow-amber)]"
                          : "border-[var(--border)] bg-[var(--bg-1)] hover:border-[var(--border-strong)]"
                      }`}
                    >
                      <span className="grid h-10 w-10 place-items-center rounded-[var(--radius-control)] bg-[var(--bg-2)] text-[var(--accent)]">
                        <Icon className="h-5 w-5" />
                      </span>
                      <h3 className="type-card-title mt-4">{template.title}</h3>
                      <p className="type-body-sm mt-1 text-[var(--fg-1)]">{template.description}</p>
                      <p className="type-caption mt-3 text-[var(--fg-2)]">{template.detail}</p>
                    </button>
                  );
                })}
              </div>
            </section>
            <aside className="border-t border-[var(--border)] pt-5 lg:border-l lg:border-t-0 lg:pl-6 lg:pt-0">
              <h2 className="type-overline text-[var(--fg-2)]">画布信息</h2>
              <Input
                wrapperClassName="mt-3"
                label="名称"
                value={title}
                maxLength={255}
                onChange={(event) => setTitle(event.currentTarget.value)}
              />
              <div className="mt-5 border-y border-[var(--border-subtle)] py-4">
                <p className="type-caption text-[var(--fg-2)]">已选模板</p>
                <p className="type-body-sm mt-1 text-[var(--fg-0)]">
                  {TEMPLATES.find((template) => template.key === selected)?.title}
                </p>
              </div>
              <Button
                className="mt-5"
                fullWidth
                variant="primary"
                size="lg"
                loading={create.isPending}
                disabled={!title.trim()}
                leftIcon={<ImageIcon className="h-4 w-4" />}
                onClick={async () => {
                  try {
                    const canvas = await create.mutateAsync({
                      title: title.trim(),
                      template: selected,
                      graph: createCanvasTemplateGraph(selected),
                    });
                    router.push(`/projects/canvas/${canvas.id}`);
                  } catch (error) {
                    toast.error(error instanceof Error ? error.message : "创建失败");
                  }
                }}
              >
                创建画布
              </Button>
            </aside>
          </div>
        </div>
      </main>
      <ProjectMobileTabBar />
    </div>
  );
}
