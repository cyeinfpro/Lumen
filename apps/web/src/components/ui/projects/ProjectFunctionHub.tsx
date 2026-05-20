"use client";

// 项目功能中心：这里是任务模板中心，同时露出最近项目，避免 poster 项目只能新建不能继续。

import {
  ArrowRight,
  ArrowUpRight,
  Film,
  Image as ImageIcon,
  Loader2,
  Palette,
  Shirt,
} from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import type { WorkflowRunListItem } from "@/lib/apiClient";
import { useWorkflowsQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { OnlineBanner } from "./components/OnlineBanner";
import { ProjectMobileTabBar, ProjectMobileTopBar, ProjectTopBar } from "./components/ProjectTopBar";
import { STATUS_LABEL } from "./types";
import { formatRelativeTime } from "./utils";

const FEATURES = [
  {
    title: "服饰模特图",
    en: "Apparel Studio",
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
    badge: "Production",
    workflowType: "apparel_model_showcase",
  },
  {
    title: "海报制作",
    en: "Poster",
    description: "从商品素材、风格和营销文案生成主视觉，再导出多尺寸成品。",
    flow: "上传商品/素材 → 选择风格 → 生成母版 → 多尺寸导出",
    input: "文案 + 风格 + 可选品牌资产",
    output: "1:1 / 4:5 / 9:16 / 16:9 海报",
    eta: "约 2-6 分钟",
    primaryHref: "/projects/poster-design/new",
    primaryLabel: "开始海报项目",
    icon: ImageIcon,
    available: true,
    badge: "Beta",
    workflowType: "poster_design",
  },
  {
    title: "风格库",
    en: "Style Library",
    description: "管理海报视觉风格预设，为海报项目准备可复用的视觉方向。",
    flow: "选择预设 → 生成样图 → 保存风格 → 用于海报",
    input: "风格描述或参考方向",
    output: "海报风格资产",
    eta: "按需维护",
    primaryHref: "/poster-styles",
    primaryLabel: "打开风格库",
    icon: Palette,
    available: true,
    badge: "Asset",
  },
  {
    title: "分镜制作",
    en: "Storyboard",
    description: "将商品卖点拆成镜头脚本与画面分镜。",
    flow: "脚本 → 镜头规划 → 分镜图 → 导出",
    input: "脚本或卖点",
    output: "分镜图与镜头说明",
    eta: "规划中",
    icon: Film,
    available: false,
    badge: "Soon",
  },
] as const;

export function ProjectFunctionHub() {
  const workflowsQuery = useWorkflowsQuery({ limit: 8 });
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
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar title="项目" subtitle="TEMPLATES · RECENT" />
      <ProjectTopBar />

      <main className="lumen-studio-bg project-mobile-scroll mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pt-2 md:mb-0 md:px-6 md:pb-6 md:pt-3">
        <div className="mx-auto grid w-full max-w-[1440px] gap-3">
          <header className="hidden min-w-0 items-center justify-between gap-3 border-b border-[var(--border)] pb-1.5 md:flex">
            <div className="flex min-w-0 items-baseline gap-2.5">
              <p className="type-page-kicker shrink-0">
                Project Hub
              </p>
              <h1 className="type-page-title shrink-0">
                项目
              </h1>
              <p className="type-page-subtitle hidden min-w-0 truncate lg:block">
                选择有步骤、有交付物的创作任务；也可以从最近项目继续 poster 或服饰流程。
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <span className="inline-flex h-7 items-baseline gap-1.5 border border-[var(--border-subtle)] px-2">
                <span className="text-[13px] font-semibold tabular-nums leading-[1.9] text-[var(--fg-0)]">
                  {String(FEATURES.filter((feature) => feature.available).length).padStart(2, "0")}
                </span>
                <span className="text-[10px] text-[var(--fg-2)]">可用</span>
              </span>
              <span className="inline-flex h-7 items-baseline gap-1.5 border border-[var(--border-subtle)] px-2">
                <span className="text-[13px] font-semibold tabular-nums leading-[1.9] text-[var(--fg-0)]">
                  {String(FEATURES.length).padStart(2, "0")}
                </span>
                <span className="text-[10px] text-[var(--fg-2)]">全部</span>
              </span>
            </div>
          </header>

          <section className="grid gap-2 border-b border-[var(--border)] pb-3 md:hidden">
            <p className="type-page-kicker">Project Hub</p>
            <h1 className="type-page-title">任务模板中心</h1>
            <p className="text-[13px] leading-[1.6] text-[var(--fg-1)]">
              从服饰模特图、海报制作或风格资产开始；已有项目可在下方继续。
            </p>
          </section>

          <section className="grid gap-x-5 gap-y-6 sm:grid-cols-2 md:grid-cols-3 md:gap-x-5 md:gap-y-7">
            {FEATURES.map((feature, index) => (
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
          </section>

          <RecentProjects
            items={recentProjects}
            loading={workflowsQuery.isLoading}
            error={workflowsQuery.isError}
            onRetry={() => workflowsQuery.refetch()}
          />
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
  const secondaryHref =
    "secondaryHref" in feature && feature.secondaryHref
      ? feature.secondaryHref
      : recentProject
        ? `/projects/${recentProject.id}`
        : "#recent-projects";
  const secondaryLabel =
    "secondaryLabel" in feature && feature.secondaryLabel
      ? feature.secondaryLabel
      : recentProject
        ? "继续最近"
        : "查看最近";

  return (
    <article
      className={cn(
        "group grid min-w-0 gap-3 focus-within:outline-none",
        feature.available ? "" : "opacity-70",
      )}
      aria-disabled={feature.available ? undefined : "true"}
    >
      <div
        className={cn(
          "relative h-24 overflow-hidden rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)] transition-all duration-[var(--dur-base)] sm:h-36 md:h-40",
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
            className="absolute right-3 top-3 inline-flex h-8 w-8 items-center justify-center rounded-[var(--radius-pill)] border border-[var(--border)] bg-[var(--bg-0)]/40 backdrop-blur transition-all duration-[var(--dur-base)] group-hover:border-[var(--border-amber)] group-hover:bg-[var(--accent)] group-hover:text-black"
          >
            <ArrowUpRight className="h-3.5 w-3.5" />
          </span>
        ) : (
          <span className="absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-[var(--radius-pill)] border border-[var(--border)] bg-[var(--bg-0)]/40 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)] backdrop-blur">
            {feature.badge}
          </span>
        )}
      </div>
      <div>
        <div className="flex items-baseline gap-2">
          <h2
            className={cn(
              "type-card-title transition-colors duration-[var(--dur-base)] sm:text-[18px] md:text-[19px]",
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
          {feature.available ? (
            <span className="ml-auto shrink-0 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
              {feature.badge}
            </span>
          ) : null}
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
      <div className="grid gap-1.5 border-t border-[var(--border)] pt-3 text-[12px] leading-[1.55] text-[var(--fg-1)]">
        <MetaLine label="流程" value={feature.flow} />
        <MetaLine label="输入" value={feature.input} />
        <MetaLine label="输出" value={feature.output} />
        <MetaLine label="耗时" value={feature.eta} />
      </div>
      <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2">
        {feature.available && "primaryHref" in feature ? (
          <Link
            href={feature.primaryHref}
            className="inline-flex min-h-9 items-center justify-center gap-1.5 bg-[var(--accent)] px-3 text-[12px] font-medium text-black transition-[transform,box-shadow] duration-[var(--dur-base)] hover:scale-[1.01] active:scale-[0.98]"
          >
            {feature.primaryLabel}
            <ArrowRight className="h-3.5 w-3.5" />
          </Link>
        ) : (
          <span className="inline-flex min-h-9 items-center justify-center border border-[var(--border)] px-3 text-[12px] text-[var(--fg-2)]">
            暂未开放
          </span>
        )}
        {feature.available ? (
          <Link
            href={secondaryHref}
            className="inline-flex min-h-9 items-center justify-center gap-1.5 border border-[var(--border)] px-3 text-[12px] font-medium text-[var(--fg-0)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-1)]"
          >
            {secondaryLabel}
          </Link>
        ) : (
          <span className="inline-flex min-h-9 items-center justify-center border border-[var(--border-subtle)] px-3 text-[12px] text-[var(--fg-3)]">
            规划中
          </span>
        )}
      </div>
    </article>
  );
}

function MetaLine({ label, value }: { label: string; value: string }) {
  return (
    <p className="grid grid-cols-[3.5em_minmax(0,1fr)] gap-2">
      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
        {label}
      </span>
      <span className="min-w-0">{value}</span>
    </p>
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
          <p className="type-page-kicker">Recent Projects</p>
          <h2 className="type-section-title mt-1">最近项目</h2>
        </div>
        <Link
          href="/projects/apparel-model-showcase"
          className="hidden shrink-0 items-center gap-1.5 border border-[var(--border)] px-3 py-2 text-[12px] font-medium text-[var(--fg-0)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-1)] sm:inline-flex"
        >
          服饰历史
        </Link>
      </div>

      <div className="mt-3 border-y border-[var(--border)]">
        {loading ? (
          <div className="flex min-h-24 items-center justify-center gap-2 text-[var(--fg-2)]">
            <Loader2 className="h-4 w-4 animate-spin" />
            <span className="font-mono text-[10px] uppercase tracking-[0.18em]">
              Loading
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
          <ul className="divide-y divide-[var(--border)]">
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
    <li className="grid gap-3 py-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
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
          <h3 className="mt-1 truncate text-[15px] font-medium tracking-tight text-[var(--fg-0)]">
            {item.title || info.fallbackTitle}
          </h3>
          <p className="mt-1 truncate text-[12.5px] text-[var(--fg-2)]">
            {item.next_action} · {updatedAt}
          </p>
        </div>
      </div>
      <Link
        href={`/projects/${item.id}`}
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
    return { label: "Poster", fallbackTitle: "海报项目", Icon: ImageIcon };
  }
  if (type === "apparel_model_showcase") {
    return { label: "Apparel", fallbackTitle: "服饰模特图", Icon: Shirt };
  }
  return { label: "Project", fallbackTitle: "项目", Icon: Palette };
}
