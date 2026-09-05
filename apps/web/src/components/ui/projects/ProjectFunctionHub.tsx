"use client";

// 项目功能中心：这里是任务模板中心，同时露出最近项目，避免 poster 项目只能新建不能继续。

import {
  ArrowRight,
  ChevronRight,
  Film,
  FolderKanban,
  Image as ImageIcon,
  Loader2,
  Palette,
  Shirt,
  Workflow,
} from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { useMemo } from "react";

import { Button } from "@/components/ui/primitives/Button";
import type { WorkflowRunListItem } from "@/lib/apiClient";
import { useWorkflowsQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { useUiStore } from "@/store/useUiStore";
import { OnlineBanner } from "./components/OnlineBanner";
import { ProjectMobileTabBar, ProjectMobileTopBar, ProjectTopBar } from "./components/ProjectTopBar";
import { STAGES as STORYBOARD_STAGES } from "./storyboard/StoryboardDomain";
import { STATUS_TEXT as STORYBOARD_STATUS_LABEL } from "./storyboard/StoryboardShared";
import { POSTER_STEPS, STATUS_LABEL, STEPS } from "./types";
import { formatRelativeTime, productThumbSrc } from "./utils";

const FEATURES = [
  {
    title: "无限画布",
    en: "自由工作流",
    description: "自由连接提示词、素材、图片与视频节点，从任意结果继续分支。",
    flow: "搭图 → 调参 → 运行节点 → 选择版本 → 交付",
    input: "提示词 / 图片 / 视频",
    output: "图片、视频与最终交付",
    eta: "按节点运行",
    primaryHref: "/projects/canvas/new",
    primaryLabel: "新建画布",
    secondaryHref: "/projects/canvas",
    secondaryLabel: "查看画布",
    icon: Workflow,
    available: true,
    badge: "自由",
    featureFlag: "canvas",
  },
  {
    title: "服饰模特图",
    en: "服饰工作流",
    description: "商品图换成可交付的模特展示图，适合电商主图、上新图和内容种草。",
    flow: "上传商品图 → 选择模特 → 生成展示图 → QC → 交付",
    input: "商品图 1-3 张",
    output: "模特展示图 + 质检交付",
    eta: "约 3-8 分钟",
    primaryHref: "/projects/apparel-model-showcase/new",
    primaryLabel: "开始服饰项目",
    secondaryHref: "/projects/apparel-model-showcase",
    secondaryLabel: "查看历史",
    icon: Shirt,
    available: true,
    badge: "正式",
    workflowType: "apparel_model_showcase",
  },
  {
    title: "海报制作",
    en: "海报工作流",
    description: "从商品素材、风格和营销文案生成主视觉，再导出多尺寸成品。",
    flow: "上传商品/素材 → 选择风格 → 生成母版 → 多尺寸导出",
    input: "文案 + 风格 + 可选品牌素材",
    output: "1:1 / 4:5 / 9:16 / 16:9 海报",
    eta: "约 2-6 分钟",
    primaryHref: "/projects/poster-design/new",
    primaryLabel: "开始海报项目",
    icon: ImageIcon,
    available: true,
    badge: "测试",
    workflowType: "poster_design",
  },
  {
    title: "风格库",
    en: "风格素材",
    description: "管理海报视觉风格预设，为海报项目准备可复用的视觉方向。",
    flow: "选择预设 → 生成样图 → 保存风格 → 用于海报",
    input: "风格描述或参考方向",
    output: "海报风格素材",
    eta: "按需维护",
    primaryHref: "/poster-styles",
    primaryLabel: "打开风格库",
    icon: Palette,
    available: true,
    badge: "素材",
  },
  {
    title: "分镜制作",
    en: "视频分镜工作流",
    description: "把想法扩写成可恢复的分镜项目，管理设定图、分镜图、视频段和最终成片。",
    flow: "想法 → 脚本 → 设定 → 分镜图 → 视频 → 成片",
    input: "想法 / 脚本 / 参考素材",
    output: "一致性分镜图、逐镜头视频与成片",
    eta: "按镜头数",
    primaryHref: "/projects/storyboard",
    primaryLabel: "打开分镜项目",
    icon: Film,
    available: true,
    badge: "高级",
    workflowType: "storyboard",
  },
] as const;

function getFeatureCardNavigation(
  feature: (typeof FEATURES)[number],
  recentProject?: WorkflowRunListItem,
): { href: string; label: string } | null {
  if ("secondaryHref" in feature && feature.secondaryHref) {
    return {
      href: feature.secondaryHref,
      label: feature.secondaryLabel,
    };
  }
  const recentHref = recentProject ? projectHref(recentProject) : null;
  return recentHref ? { href: recentHref, label: "继续最近" } : null;
}

export function ProjectFunctionHub() {
  const workflowsQuery = useWorkflowsQuery({ limit: 8 });
  const canvasEnabled = useUiStore((state) => state.canvasEnabled);
  const visibleFeatures = useMemo(
    () =>
      FEATURES.filter(
        (feature) =>
          !("featureFlag" in feature) ||
          feature.featureFlag !== "canvas" ||
          canvasEnabled,
      ),
    [canvasEnabled],
  );
  const recentProjects = useMemo(
    () => workflowsQuery.data?.items ?? [],
    [workflowsQuery.data?.items],
  );
  const recentByType = useMemo(() => {
    const map = new Map<string, WorkflowRunListItem>();
    for (const item of recentProjects) {
      if (!map.has(item.type)) map.set(item.type, item);
    }
    return map;
  }, [recentProjects]);

  return (
    <div className="page-shell relative h-[100dvh]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar title="创作工作流" subtitle="商业工作流 · 最近项目" />
      <ProjectTopBar />

      <main className="page-scroll lumen-studio-bg project-mobile-scroll mb-[var(--mobile-tabbar-height)]">
        <h1 className="sr-only md:hidden">创作工作流</h1>
        <div className="page-frame grid gap-6 py-4">
          <div className="hidden md:block">
            <header className="page-header">
              <div className="page-header-copy">
                <p className="type-page-kicker">LUMEN WORKFLOWS</p>
                <h1 className="type-page-title text-[var(--fg-0)]">
                  创作工作流
                </h1>
                <p className="type-page-subtitle max-w-2xl text-[var(--fg-2)]">
                  选择适合的商业创作流，或继续未完成的工作。
                </p>
              </div>
            </header>
          </div>

          <RecentProjects
            items={recentProjects}
            loading={workflowsQuery.isLoading}
            error={workflowsQuery.isError}
            onRetry={() => workflowsQuery.refetch()}
          />

          <section className="grid gap-3 pt-2">
            <div className="flex min-w-0 items-end justify-between gap-3">
              <div>
                <h2 className="type-section-title text-[var(--fg-0)]">商业功能矩阵</h2>
                <p className="mt-0.5 type-caption text-[var(--fg-2)]">
                  全流程针对性加速与自动化质检，选择模板立即开启
                </p>
              </div>
            </div>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
              {visibleFeatures.map((feature, index) => (
                <FeatureMatrixCard
                  key={feature.title}
                  feature={feature}
                  index={index}
                  recentProject={
                    "workflowType" in feature && feature.workflowType
                      ? recentByType.get(feature.workflowType)
                      : undefined
                  }
                />
              ))}
            </div>
          </section>
        </div>
      </main>

      <ProjectMobileTabBar />
    </div>
  );
}

function FeatureMatrixCard({
  feature,
  index,
  recentProject,
}: {
  feature: (typeof FEATURES)[number];
  index: number;
  recentProject?: WorkflowRunListItem;
}) {
  const Icon = feature.icon;
  const num = `N°${String(index + 1).padStart(2, "0")}`;
  const steps = feature.flow.split("→").map((step) => step.trim()).filter(Boolean);
  const metrics = [
    ["输入", feature.input],
    ["输出", feature.output],
    ["耗时", feature.eta],
  ] as const;
  const recentStatus = recentProject
    ? STATUS_LABEL[recentProject.status] ?? recentProject.status
    : null;
  const secondaryNavigation = getFeatureCardNavigation(feature, recentProject);

  return (
    <article
      className={cn(
        "group surface-card-v2 relative flex min-w-0 flex-col justify-between p-5",
        !feature.available && "pointer-events-none opacity-60",
      )}
      aria-disabled={feature.available ? undefined : "true"}
    >
      <div>
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)] transition-colors duration-200 group-hover:border-accent-border group-hover:text-[var(--accent)]">
              <Icon className="h-5 w-5" strokeWidth={1.8} />
            </span>
            <div className="min-w-0">
              <p className="truncate type-caption tabular-nums text-[var(--fg-3)]">
                {num} · {feature.en}
              </p>
              <h3 className="mt-0.5 type-card-title text-[var(--fg-0)]">
                {feature.title}
              </h3>
            </div>
          </div>
          <span
            className={cn(
              "shrink-0 rounded-full border px-2.5 py-0.5 type-caption font-medium",
              feature.badge === "正式"
                ? "border-accent-border bg-accent-soft !text-[var(--fg-0)]"
                : feature.badge === "自由"
                  ? "border-info-border bg-info-soft !text-[var(--fg-0)]"
                  : "border-[var(--border-subtle)] bg-[var(--bg-2)] !text-[var(--fg-2)]",
            )}
          >
            {feature.badge}
          </span>
        </div>

        <p
          className={cn(
            "mt-3.5 min-w-0 type-body-sm leading-6",
            feature.available ? "text-[var(--fg-1)]" : "text-[var(--fg-3)]",
          )}
        >
          {feature.description}
        </p>

        <div className="mt-3.5 flex flex-wrap items-center gap-1.5" aria-label="工作流步骤">
          {steps.map((step, stepIndex) => (
            <span key={`${step}-${stepIndex}`} className="inline-flex items-center gap-1">
              <span className="inline-flex items-center gap-1 rounded-full border border-[var(--border-subtle)] bg-[var(--bg-0)]/60 px-2 py-0.5 type-caption text-[var(--fg-2)]">
                <span className="tabular-nums text-[var(--fg-3)]">{stepIndex + 1}</span>
                <span>{step}</span>
              </span>
              {stepIndex < steps.length - 1 ? (
                <ChevronRight className="h-3.5 w-3.5 shrink-0 text-[var(--fg-3)]" aria-hidden />
              ) : null}
            </span>
          ))}
        </div>

        <dl className="mt-4 grid grid-cols-3 border-y border-[var(--border-subtle)] py-3">
          {metrics.map(([label, value], metricIndex) => (
            <div
              key={label}
              className={cn(
                "min-w-0 px-2",
                metricIndex > 0 && "border-l border-[var(--border-subtle)]",
                metricIndex === 0 && "pl-0",
                metricIndex === metrics.length - 1 && "pr-0",
              )}
            >
              <dt className="type-caption text-[var(--fg-3)]">{label}</dt>
              <dd className="mt-1 line-clamp-2 type-caption font-medium text-[var(--fg-1)]" title={value}>
                {value}
              </dd>
            </div>
          ))}
        </dl>
      </div>

      <div className="mt-5 border-t border-[var(--border-subtle)] pt-3.5">
        {recentProject ? (
          <p className="mb-2.5 flex min-w-0 items-center gap-1.5 type-caption text-[var(--fg-2)]">
            <FolderKanban className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
            <span className="truncate">
              最近：{recentProject.title || feature.title} · {recentStatus}
            </span>
          </p>
        ) : null}

        <FeatureCardActions
          feature={feature}
          secondaryNavigation={secondaryNavigation}
        />
      </div>
    </article>
  );
}

function FeatureCardActions({
  feature,
  secondaryNavigation,
}: {
  feature: (typeof FEATURES)[number];
  secondaryNavigation: { href: string; label: string } | null;
}) {
  if (!feature.available || !("primaryHref" in feature)) {
    return (
      <span className="inline-flex min-h-10 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border-subtle)] px-3 type-body-sm text-[var(--fg-3)]">
        暂未开放
      </span>
    );
  }

  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-2",
        secondaryNavigation && "min-[360px]:grid-cols-2",
      )}
    >
      <Link
        href={feature.primaryHref}
        className="inline-flex min-h-10 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-accent-border bg-accent-soft px-3 type-body-sm font-medium text-[var(--fg-0)] shadow-[var(--shadow-1)] transition-[transform,background-color,border-color,box-shadow] hover:bg-[var(--bg-2)] hover:shadow-[var(--shadow-amber)] active:scale-[0.98]"
      >
        <span>{feature.primaryLabel}</span>
        <ArrowRight className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
      </Link>

      {secondaryNavigation ? (
        <Link
          href={secondaryNavigation.href}
          className="inline-flex min-h-10 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]/60 px-3 type-body-sm font-medium text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]"
        >
          {secondaryNavigation.label}
        </Link>
      ) : null}
    </div>
  );
}

function RecentProjects({
  items,
  loading,
  error,
  onRetry,
}: {
  items: WorkflowRunListItem[];
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}) {
  return (
    <section id="recent-projects" className="grid gap-3">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <h2 className="type-section-title text-[var(--fg-0)]">最近项目</h2>
        <Link
          href="/projects/apparel-model-showcase"
          aria-label="查看服饰项目历史"
          className="inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] px-3 type-caption font-medium text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-1)] hover:text-[var(--fg-0)]"
        >
          服饰历史
          <ArrowRight className="h-3.5 w-3.5" />
        </Link>
      </div>

      {loading ? (
        <div className="flex min-h-24 items-center justify-center gap-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/50 text-[var(--fg-2)]">
          <Loader2 className="h-4 w-4 animate-spin text-[var(--accent)]" />
          <span className="type-caption">加载中</span>
        </div>
      ) : error ? (
        <div className="flex min-h-24 flex-col items-center justify-center gap-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/50 p-4 text-center">
          <p className="type-body-sm text-[var(--fg-2)]">最近项目加载失败</p>
          <Button type="button" variant="secondary" size="sm" onClick={onRetry}>
            重试
          </Button>
        </div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/50 px-6 py-8 text-center backdrop-blur-sm">
          <span className="flex h-10 w-10 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)] shadow-[var(--shadow-1)]">
            <FolderKanban className="h-5 w-5 text-[var(--accent)]" strokeWidth={1.8} />
          </span>
          <p className="mt-3 type-body-sm font-medium text-[var(--fg-0)]">暂无最近项目</p>
          <p className="mt-1 max-w-sm type-caption text-[var(--fg-2)]">
            从下方选择一个工作流开始创作，项目会自动保存在这里。
          </p>
        </div>
      ) : (
        <ul aria-label="最近项目" className="divide-y divide-[var(--border-subtle)] border-y border-[var(--border-subtle)]">
          {items.map((item, index) => (
            <RecentProjectCard key={item.id} item={item} priority={index === 0} />
          ))}
        </ul>
      )}
    </section>
  );
}

function RecentProjectCard({
  item,
  priority = false,
}: {
  item: WorkflowRunListItem;
  priority?: boolean;
}) {
  const info = workflowTypeInfo(item.type);
  const Icon = info.Icon;
  const title = item.title || info.fallbackTitle;
  const statusLabel = workflowStatusLabel(item);
  const stageLabel = workflowStageLabel(item);
  const updatedAt = formatRelativeTime(item.updated_at);
  const previewSrc = productThumbSrc(item);
  const href = projectHref(item);
  const isCompleted = item.status === "completed";

  return (
    <li className="group relative flex min-w-0 items-start gap-3 py-3">
      <div
        className="relative h-16 w-16 shrink-0 overflow-hidden rounded-[var(--radius-control)] bg-[var(--bg-2)]"
        data-preview-state={previewSrc ? "available" : "empty"}
      >
        {previewSrc ? (
          <Image
            src={previewSrc}
            alt={`${title}的项目素材`}
            fill
            priority={priority}
            sizes="64px"
            unoptimized
            className="object-cover transition-transform duration-300 group-hover:scale-[1.02] motion-reduce:transform-none"
          />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-[var(--fg-3)]">
            <Icon className="h-5 w-5" strokeWidth={1.6} />
            <span className="type-caption">暂无预览</span>
          </div>
        )}
        <span className="sr-only">
          <Icon className="h-3.5 w-3.5" />
          {info.label}
        </span>
      </div>

      <div className="grid min-w-0 flex-1 gap-2 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-center">
        <div className="flex min-w-0 flex-wrap items-start gap-2">
          <h3 className="line-clamp-2 min-w-0 type-card-title text-[var(--fg-0)]">
            {href ? <Link href={href}>{title}</Link> : title}
          </h3>
          <span
            data-project-status={item.status}
            className={cn(
              "shrink-0 rounded-full border px-2 py-0.5 type-caption font-medium",
              projectStatusTone(item.status),
            )}
          >
            {statusLabel}
          </span>
        </div>

        <div className="flex min-w-0 items-center gap-3">
          <ProjectProgressRing value={item.completion_percent} />
          <div className="min-w-0">
            <p className="type-caption text-[var(--fg-3)]">当前阶段</p>
            <p
              data-project-progress
              className="truncate type-caption text-[var(--fg-1)]"
              title={stageLabel}
            >
              {stageLabel}
            </p>
          </div>
        </div>

        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 type-caption md:col-start-1 md:row-start-2">
          {item.next_action ? (
            <>
              <dt className="text-[var(--fg-3)]">下一步</dt>
              <dd className="truncate text-[var(--fg-1)]" title={item.next_action}>
                {item.next_action}
              </dd>
            </>
          ) : null}
          {item.output_count > 0 ? (
            <>
              <dt className="text-[var(--fg-3)]">产出</dt>
              <dd className="text-[var(--fg-1)]">{item.output_count} 个</dd>
            </>
          ) : null}
        </dl>

        <div className="min-w-0 md:col-start-3 md:row-span-2 md:row-start-1">
          <time dateTime={item.updated_at} className="block type-caption text-[var(--fg-2)]">
            更新于 {updatedAt}
          </time>
          {href ? (
            <Link
              href={href}
              className="mt-3 inline-flex min-h-9 w-full items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)]/70 px-3 type-caption font-medium text-[var(--fg-0)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-3)]"
            >
              {isCompleted ? "查看交付" : "继续项目"}
              <ArrowRight className="h-3.5 w-3.5" />
            </Link>
          ) : (
            <span className="mt-3 inline-flex min-h-9 w-full items-center justify-center rounded-[var(--radius-control)] border border-[var(--border-subtle)] px-3 type-caption text-[var(--fg-3)]">
              暂不支持
            </span>
          )}
        </div>
      </div>
    </li>
  );
}

function ProjectProgressRing({ value }: { value: number }) {
  return (
    <span
      role="progressbar"
      aria-label={`项目完成度 ${value}%`}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value}
      data-project-progress-ring
      className="relative inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-full"
      style={{
        background: `conic-gradient(var(--accent) ${value}%, var(--bg-3) 0)`,
      }}
    >
      <span className="absolute inset-[3px] rounded-full bg-[var(--bg-1)]" aria-hidden />
      <span className="relative type-caption font-medium tabular-nums text-[var(--fg-0)]">
        {value}%
      </span>
    </span>
  );
}

const APPAREL_STAGE_LABELS = Object.fromEntries(
  STEPS.map((step) => [step.key, step.label]),
) as Record<string, string>;
const POSTER_STAGE_LABELS = Object.fromEntries(
  POSTER_STEPS.map((step) => [step.key, step.label]),
) as Record<string, string>;
const STORYBOARD_STAGE_LABELS = Object.fromEntries(
  STORYBOARD_STAGES.map((stage) => [stage.id, stage.label]),
) as Record<string, string>;

function workflowStageLabel(item: WorkflowRunListItem): string {
  const labels =
    item.type === "apparel_model_showcase"
      ? APPAREL_STAGE_LABELS
      : item.type === "poster_design"
        ? POSTER_STAGE_LABELS
        : item.type === "storyboard"
          ? STORYBOARD_STAGE_LABELS
          : undefined;
  return (labels?.[item.current_step] ?? item.current_step) || "未提供";
}

function workflowStatusLabel(item: WorkflowRunListItem): string {
  const labels = item.type === "storyboard" ? STORYBOARD_STATUS_LABEL : STATUS_LABEL;
  return labels[item.status] ?? item.status;
}

function projectStatusTone(status: string): string {
  if (status === "completed") {
    return "border-success-border bg-success-soft !text-[var(--success-fg)]";
  }
  if (status === "failed") {
    return "border-danger-border bg-danger-soft !text-[var(--danger-fg)]";
  }
  if (["running", "in_progress", "generating", "compositing"].includes(status)) {
    return "border-accent-border bg-accent-soft !text-[var(--fg-0)]";
  }
  if (status === "needs_review" || status === "waiting_input") {
    return "border-warning-border bg-warning-soft !text-[var(--warning-fg)]";
  }
  return "border-[var(--border-subtle)] bg-[var(--bg-2)] !text-[var(--fg-2)]";
}

function workflowTypeInfo(type: string): {
  label: string;
  fallbackTitle: string;
  Icon: typeof Shirt;
} {
  if (type === "poster_design") {
    return { label: "海报", fallbackTitle: "海报项目", Icon: ImageIcon };
  }
  if (type === "apparel_model_showcase") {
    return { label: "服饰", fallbackTitle: "服饰模特图", Icon: Shirt };
  }
  if (type === "storyboard") {
    return { label: "分镜", fallbackTitle: "分镜项目", Icon: Film };
  }
  return { label: "项目", fallbackTitle: "未命名项目", Icon: Palette };
}

function projectHref(item: WorkflowRunListItem): string | null {
  if (item.type === "apparel_model_showcase" || item.type === "poster_design") {
    return `/projects/${encodeURIComponent(item.id)}`;
  }
  if (item.type === "storyboard") {
    return `/projects/storyboard/${encodeURIComponent(item.id)}`;
  }
  return null;
}
