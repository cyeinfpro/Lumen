"use client";

// 项目功能中心：这里是任务模板中心，同时露出最近项目，避免 poster 项目只能新建不能继续。

import {
  ArrowRight,
  CheckCircle2,
  Clock3,
  Film,
  FolderKanban,
  Image as ImageIcon,
  Loader2,
  Palette,
  Shirt,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import type { WorkflowRunListItem } from "@/lib/apiClient";
import { useWorkflowsQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { useUiStore } from "@/store/useUiStore";
import { OnlineBanner } from "./components/OnlineBanner";
import { ProjectMobileTabBar, ProjectMobileTopBar, ProjectTopBar } from "./components/ProjectTopBar";
import { STATUS_LABEL } from "./types";
import { formatRelativeTime } from "./utils";

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
) {
  if ("secondaryHref" in feature && feature.secondaryHref) {
    return {
      href: feature.secondaryHref,
      label: feature.secondaryLabel,
    };
  }
  if (recentProject) {
    return {
      href: projectHref(recentProject),
      label: "继续最近",
    };
  }
  return {
    href: "#recent-projects",
    label: "查看最近",
  };
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
      <ProjectMobileTopBar title="项目" subtitle="工作流 · 最近" />
      <ProjectTopBar />

      <main className="page-scroll lumen-studio-bg project-mobile-scroll mb-[var(--mobile-tabbar-height)]">
        <div className="page-frame grid gap-4">
          <header className="page-header hidden md:grid">
            <div className="page-header-copy">
              <p className="type-page-kicker">项目工作台</p>
              <h1 className="type-page-title">
                继续最近项目，保持创作节奏
              </h1>
              <p className="type-page-subtitle max-w-2xl">
                先回到仍在进行的工作；需要开启新任务时，再从下方模板创建。
              </p>
            </div>
          </header>

          <header className="page-header grid gap-2 md:hidden">
            <p className="type-page-kicker">项目中心</p>
            <h1 className="type-page-title">项目工作台</h1>
            <p className="type-page-subtitle">
              先继续最近项目，也可以从模板开启新的工作流。
            </p>
          </header>

          <RecentProjects
            items={recentProjects}
            loading={workflowsQuery.isLoading}
            error={workflowsQuery.isError}
            onRetry={() => workflowsQuery.refetch()}
          />

          <section className="grid gap-3 border-t border-[var(--border-subtle)] pt-4">
            <div className="flex min-w-0 items-end justify-between gap-3">
              <div className="min-w-0">
                <p className="type-page-kicker">工作流模板</p>
                <h2 className="type-section-title mt-1">选择工作流</h2>
              </div>
              <p className="hidden text-xs text-[var(--fg-2)] md:block">
                每张卡片都可以直接新建，或接回对应的最近项目。
              </p>
            </div>
            <div className="grid gap-0">
              {visibleFeatures.map((feature, index) => (
                <FeatureCard
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

function FeatureCard({
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
  const recentStatus = recentProject
    ? STATUS_LABEL[recentProject.status] ?? recentProject.status
    : null;
  const { href: secondaryHref, label: secondaryLabel } =
    getFeatureCardNavigation(feature, recentProject);

  return (
    <article
      className={cn(
        "group grid min-w-0 gap-4 border-t border-[var(--border)] py-4 transition-colors duration-[var(--dur-base)] md:grid-cols-[minmax(0,1.05fr)_minmax(0,1.15fr)_auto] md:items-center md:gap-6 md:py-5",
        feature.available
          ? "hover:bg-[var(--bg-1)]/45"
          : "opacity-60",
      )}
      aria-disabled={feature.available ? undefined : "true"}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center border border-[var(--border)] bg-[var(--bg-0)] text-[var(--accent)]">
            <Icon className="h-5 w-5" strokeWidth={1.7} />
          </span>
          <div className="min-w-0">
            <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-3)]">
              {num} · {feature.en}
            </p>
            <h2 className="type-section-title mt-1">
              {feature.title}
            </h2>
          </div>
        </div>
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
          {feature.badge}
        </span>
      </div>

      <p
        className={cn(
          "min-w-0 text-[13px] leading-6 md:line-clamp-3",
          feature.available ? "text-[var(--fg-1)]" : "text-[var(--fg-3)]",
        )}
      >
        {feature.description}
      </p>

      <div className="grid gap-3 border-y border-[var(--border-subtle)] py-3 md:border-y-0 md:border-l md:pl-5">
        <div className="grid grid-cols-3 gap-2">
          <FeatureDatum label="输入" value={feature.input} />
          <FeatureDatum label="输出" value={feature.output} />
          <FeatureDatum label="耗时" value={feature.eta} />
        </div>
        <WorkflowSteps steps={steps} disabled={!feature.available} />
      </div>

      <div className="min-h-8 text-xs text-[var(--fg-2)] md:hidden">
        {recentProject ? (
          <span className="flex min-w-0 items-center gap-2">
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-[var(--success-fg)]" />
            <span className="truncate">
              最近：{recentProject.title || feature.title} · {recentStatus}
            </span>
          </span>
        ) : feature.available ? (
          <span className="flex min-w-0 items-center gap-2">
            <FolderKanban className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
            <span className="truncate">可直接新建，也会自动进入最近项目。</span>
          </span>
        ) : (
          <span className="flex min-w-0 items-center gap-2">
            <Clock3 className="h-3.5 w-3.5 shrink-0 text-[var(--fg-3)]" />
            <span className="truncate">暂未开放，保留为未来视频/脚本流程入口。</span>
          </span>
        )}
      </div>

      <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2 md:min-w-[168px]">
        {feature.available && "primaryHref" in feature ? (
          <Link
            href={feature.primaryHref}
            className="inline-flex min-h-11 items-center justify-center gap-1.5 bg-[var(--accent)] px-3 text-[13px] font-semibold text-[var(--accent-on)] shadow-[var(--shadow-1)] transition-[background-color,opacity] duration-[var(--dur-base)] hover:bg-[var(--amber-300)] active:opacity-[var(--op-press)] sm:min-h-10"
          >
            {feature.primaryLabel}
            <ArrowRight className="h-3.5 w-3.5" />
          </Link>
        ) : (
          <span className="inline-flex min-h-10 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] px-3 text-[13px] text-[var(--fg-2)]">
            暂未开放
          </span>
        )}
        {feature.available ? (
          <Link
            href={secondaryHref}
            className="inline-flex min-h-11 items-center justify-center gap-1.5 border border-[var(--border)] bg-[var(--bg-0)]/58 px-3 text-[13px] font-medium text-[var(--fg-0)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] sm:min-h-10"
          >
            {secondaryLabel}
          </Link>
        ) : (
          <span className="inline-flex min-h-10 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border-subtle)] px-3 text-[13px] text-[var(--fg-3)]">
            规划中
          </span>
        )}
      </div>
    </article>
  );
}

function FeatureDatum({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-3)]">
        {label}
      </p>
      <p className="mt-1 line-clamp-2 min-h-8 text-[12px] leading-4 text-[var(--fg-1)]">
        {value}
      </p>
    </div>
  );
}

function WorkflowSteps({ steps, disabled }: { steps: string[]; disabled: boolean }) {
  return (
    <div className="flex min-w-0 flex-wrap gap-1.5 border-t border-[var(--border-subtle)] pt-3">
      {steps.map((step, stepIndex) => (
        <span
          key={`${step}-${stepIndex}`}
          className={cn(
            "inline-flex min-h-7 max-w-full items-center gap-1.5 border-b px-1 text-[11px]",
            disabled
              ? "border-[var(--border-subtle)] text-[var(--fg-3)]"
              : "border-[var(--border)] text-[var(--fg-1)]",
          )}
        >
          <span className="font-mono text-[10px] tabular-nums text-[var(--fg-3)]">
            {stepIndex + 1}
          </span>
          <span className="truncate">{step}</span>
        </span>
      ))}
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
    <section id="recent-projects" className="mt-2 border-t border-[var(--border)] pt-4">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="type-page-kicker">最近项目</p>
          <h2 className="type-section-title mt-1">最近项目</h2>
        </div>
        <Link
          href="/projects/apparel-model-showcase"
          className="hidden shrink-0 items-center gap-1.5 border border-[var(--border)] px-3 py-2 text-[12px] font-medium text-[var(--fg-0)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-1)] sm:inline-flex"
        >
          服饰历史
        </Link>
      </div>

      <div className="list-group mt-3">
        {loading ? (
          <div className="flex min-h-24 items-center justify-center gap-2 text-[var(--fg-2)]">
            <Loader2 className="h-4 w-4 animate-spin" />
            <span className="font-mono text-[10px] uppercase tracking-[0.18em]">
              读取中
            </span>
          </div>
        ) : error ? (
          <button
            type="button"
            onClick={onRetry}
            className="flex min-h-24 w-full items-center justify-center text-[13px] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-1)]"
          >
            最近项目加载失败，点击重试
          </button>
        ) : items.length === 0 ? (
          <div className="grid min-h-24 place-items-center px-4 text-center text-[13px] leading-[1.6] text-[var(--fg-2)]">
            还没有项目。先从上方模板开始，生成后的 poster 和服饰项目都会在这里继续。
          </div>
        ) : (
          <ul>
            {items.map((item) => (
              <RecentProjectRow key={item.id} item={item} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

function RecentProjectRow({ item }: { item: WorkflowRunListItem }) {
  const info = workflowTypeInfo(item.type);
  const Icon = info.Icon;
  const statusLabel = STATUS_LABEL[item.status] ?? item.status;
  const updatedAt = formatRelativeTime(item.updated_at);
  return (
    <li className="list-row grid gap-3 px-0 py-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
      <div className="flex min-w-0 gap-3">
        <div className="mt-0.5 inline-flex h-9 w-9 shrink-0 items-center justify-center border border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-1)]">
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
              {info.label}
            </span>
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
              {statusLabel}
            </span>
            {item.output_count > 0 ? (
              <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                {item.output_count} outputs
              </span>
            ) : null}
          </div>
          <h3 className="mt-1 truncate text-[15px] font-medium text-[var(--fg-0)]">
            {item.title || info.fallbackTitle}
          </h3>
          <p className="mt-1 truncate text-[12.5px] text-[var(--fg-2)]">
            {item.next_action} · {updatedAt}
          </p>
        </div>
      </div>
      <Link
        href={projectHref(item)}
        className="inline-flex min-h-9 items-center justify-center gap-1.5 border border-[var(--border)] px-3 text-[12px] font-medium text-[var(--fg-0)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-1)]"
      >
        {item.status === "completed" ? "查看交付" : "继续项目"}
        <ArrowRight className="h-3.5 w-3.5" />
      </Link>
    </li>
  );
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
  return { label: "项目", fallbackTitle: "项目", Icon: Palette };
}

function projectHref(item: WorkflowRunListItem): string {
  if (item.type === "storyboard") {
    return `/projects/storyboard/${item.id}`;
  }
  return `/projects/${item.id}`;
}
