"use client";

// Editorial 详情页 / 控制台：
// 1) 三栏（StepRail | StagePanel | ConstraintPanel），中屏改抽屉
// 2) Header：mono eyebrow + unified title + dot + mono timestamp，无嵌套卡片
// 3) AnimatePresence 阶段切换；StageErrorBoundary 兜底
// 4) ⌘/Ctrl + . 切换右侧约束面板抽屉

import { AnimatePresence, motion } from "framer-motion";
import {
  Check,
  Loader2,
  MoreHorizontal,
  PanelRightOpen,
  Pencil,
  Trash2,
  X,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { toast } from "@/components/ui/primitives/Toast";
import { useDeleteWorkflowMutation, usePatchWorkflowMutation, useWorkflowQuery } from "@/lib/queries";
import type { WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { ConstraintDrawer, ConstraintPanel } from "./components/ConstraintPanel";
import { OnlineBanner } from "./components/OnlineBanner";
import { ProjectMobileTabBar, ProjectMobileTopBar, ProjectTopBar } from "./components/ProjectTopBar";
import { StageErrorBoundary } from "./components/StageErrorBoundary";
import { MobileStageStrip, StepRail } from "./components/StepRail";
import { ProductUploadSummary } from "./stages/ProductUploadSummary";
import { ProductAnalysisStage } from "./stages/ProductAnalysisStage";
import { ModelSettingsStage } from "./stages/ModelSettingsStage";
import { ModelCandidatesStage } from "./stages/ModelCandidatesStage";
import { ShowcaseGenerationStage } from "./stages/ShowcaseGenerationStage";
import { QualityReviewStage } from "./stages/QualityReviewStage";
import { DeliveryStage } from "./stages/DeliveryStage";
import { STATUS_LABEL, STEP_INDEX, STEPS } from "./types";
import { formatRelativeTime } from "./utils";

interface DetailProps {
  projectId: string;
}

export function ApparelWorkflowDetail({ projectId }: DetailProps) {
  const query = useWorkflowQuery(projectId);
  const workflow = query.data;

  return (
    <div className="page-shell relative h-[100dvh]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="项目"
        subtitle={
          workflow ? (STATUS_LABEL[workflow.status] ?? workflow.status).toUpperCase() : "LOADING"
        }
        backHref="/projects/apparel-model-showcase"
        backLabel="返回服饰模特图"
      />
      <ProjectTopBar />

      {!workflow && query.isLoading ? (
        <DetailSkeleton />
      ) : query.isError ? (
        <DetailError onRetry={() => query.refetch()} />
      ) : !workflow ? (
        <div className="p-6 text-sm text-[var(--fg-1)]">项目加载失败</div>
      ) : (
        <ProjectConsole workflow={workflow} refreshing={query.isFetching} />
      )}
      <ProjectMobileTabBar />
    </div>
  );
}

function ProjectConsole({
  workflow,
  refreshing,
}: {
  workflow: WorkflowRun;
  refreshing: boolean;
}) {
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === ".") {
        event.preventDefault();
        setDrawerOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <main className="mb-[var(--mobile-tabbar-height)] grid min-h-0 flex-1 overflow-hidden md:mb-0 lg:grid-cols-[232px_minmax(0,1fr)] xl:grid-cols-[232px_minmax(0,1fr)_300px]">
      <aside className="hidden border-r border-[var(--border)] px-5 py-6 lg:block">
        <StepRail workflow={workflow} />
      </aside>

      <section className="page-scroll project-mobile-scroll min-h-0 min-w-0 px-3 pt-3 min-[390px]:px-4 md:px-6 md:pb-8 md:pt-3 xl:px-6">
        <DetailHeader workflow={workflow} refreshing={refreshing} onOpenDrawer={() => setDrawerOpen(true)} />
        <MobileStageStrip workflow={workflow} />

        <StageErrorBoundary resetKeys={[workflow.id, workflow.current_step]}>
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={workflow.current_step}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
            >
              <WorkflowStagePanel workflow={workflow} />
            </motion.div>
          </AnimatePresence>
        </StageErrorBoundary>

      </section>

      <aside className="hidden overflow-y-auto border-l border-[var(--border)] px-5 py-6 xl:block">
        <ConstraintPanel workflow={workflow} />
      </aside>

      <ConstraintDrawer
        workflow={workflow}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
      />
    </main>
  );
}

function DetailHeader({
  workflow,
  refreshing,
  onOpenDrawer,
}: {
  workflow: WorkflowRun;
  refreshing: boolean;
  onOpenDrawer: () => void;
}) {
  const status = workflow.status;
  const router = useRouter();
  const workflowTitle = workflow.title || "服饰模特图";
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(workflowTitle);
  const trackedWorkflowTitleRef = useRef(workflowTitle);
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const patch = usePatchWorkflowMutation({
    onSuccess: (data) => {
      setTitle(data.title || "服饰模特图");
      setEditing(false);
      toast.success("项目已重命名");
    },
    onError: (error) => toast.error(error.message || "重命名失败"),
  });
  const remove = useDeleteWorkflowMutation({
    onSuccess: () => {
      toast.success("项目已删除");
      router.push("/projects/apparel-model-showcase");
    },
    onError: (error) => toast.error(error.message || "删除失败"),
  });

  useEffect(() => {
    if (editing) return;
    if (trackedWorkflowTitleRef.current === workflowTitle) return;
    trackedWorkflowTitleRef.current = workflowTitle;
    setTitle(workflowTitle);
  }, [editing, workflowTitle]);

  const menuRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!menuOpen) return;
    const onPointer = (event: PointerEvent) => {
      if (!menuRef.current) return;
      if (menuRef.current.contains(event.target as Node)) return;
      setMenuOpen(false);
      setConfirmDelete(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        setConfirmDelete(false);
      }
    };
    document.addEventListener("pointerdown", onPointer, true);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer, true);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const saveTitle = () => {
    const next = title.trim();
    if (!next) {
      toast.error("项目名称不能为空");
      return;
    }
    if (next === workflow.title) {
      setEditing(false);
      return;
    }
    patch.mutate({ id: workflow.id, title: next });
  };

  const dotTone = useMemo(() => {
    if (status === "completed") return "bg-[var(--success)]";
    if (status === "running" || status === "needs_review")
      return "bg-[var(--amber-400)] animate-[lumen-pulse-soft_1800ms_ease-in-out_infinite]";
    if (status === "failed") return "bg-[var(--danger)]";
    return "bg-[var(--fg-3)]";
  }, [status]);

  const stepNum = String((STEP_INDEX[workflow.current_step] ?? 0) + 1).padStart(2, "0");
  const stepTotal = STEPS.length;

  return (
    <header className="page-header mb-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-center md:gap-3">
      <div className="min-w-0">
        <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
          Step {stepNum} / {String(stepTotal).padStart(2, "0")} · Apparel Project
        </p>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1.5">
          {editing ? (
            <form
              className="flex min-w-0 flex-wrap items-baseline gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                saveTitle();
              }}
            >
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.preventDefault();
                    setTitle(workflowTitle);
                    setEditing(false);
                  }
                }}
                maxLength={120}
                autoFocus
                aria-label="项目名称"
                className="type-page-title min-w-0 max-w-[min(calc(100vw_-_4rem),640px)] border-b border-[var(--border-amber)] bg-transparent px-1 outline-none"
              />
              <button
                type="submit"
                aria-label="保存项目名称"
                disabled={patch.isPending}
                className="inline-flex h-11 w-11 cursor-pointer items-center justify-center rounded-full text-[var(--fg-0)] transition-colors hover:bg-[var(--bg-3)] disabled:opacity-50 md:h-8 md:w-8"
              >
                {patch.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Check className="h-4 w-4" />
                )}
              </button>
              <button
                type="button"
                aria-label="取消重命名"
                onClick={() => {
                  setTitle(workflowTitle);
                  setEditing(false);
                }}
                className="inline-flex h-11 w-11 cursor-pointer items-center justify-center rounded-full text-[var(--fg-2)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] md:h-8 md:w-8"
              >
                <X className="h-4 w-4" />
              </button>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="group/title flex min-w-0 cursor-pointer items-baseline gap-2 text-left focus-visible:outline-none"
              aria-label="编辑项目名称"
            >
              <h1 className="type-page-title min-w-0 break-words line-clamp-2 md:line-clamp-1">
                {workflowTitle}
              </h1>
              <Pencil className="h-3.5 w-3.5 shrink-0 text-[var(--fg-3)] opacity-0 transition-opacity group-hover/title:opacity-100" />
            </button>
          )}
        </div>

        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] uppercase tracking-[0.18em]">
          <span className="inline-flex items-center gap-2 text-[var(--fg-1)]">
            <span aria-hidden className={cn("h-1.5 w-1.5 rounded-full", dotTone)} />
            {STATUS_LABEL[status] ?? status}
          </span>
          <span aria-hidden className="text-[var(--fg-3)]">·</span>
          <span className="text-[var(--fg-2)]">
            Updated {formatRelativeTime(workflow.updated_at)}
          </span>
          {refreshing ? (
            <span className="inline-flex items-center gap-1.5 text-[var(--fg-2)]">
              <Loader2 className="h-3 w-3 animate-spin" />
              Syncing
            </span>
          ) : null}
        </div>

        {workflow.user_prompt ? (
          <p className="type-page-subtitle mt-1.5 line-clamp-1 max-w-2xl text-[var(--fg-1)]">
            {workflow.user_prompt}
          </p>
        ) : null}
      </div>

      <div className="flex min-w-0 flex-wrap items-center gap-2 self-start md:self-center">
        <button
          type="button"
          onClick={onOpenDrawer}
          aria-label="查看项目约束 (⌘ .)"
          className="inline-flex min-h-11 cursor-pointer items-center gap-1.5 border border-[var(--border)] px-2.5 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-amber)] hover:text-[var(--amber-300)] md:min-h-8 xl:hidden"
        >
          <PanelRightOpen className="h-3.5 w-3.5" />
          Constraints
        </button>
        <div className="relative" ref={menuRef}>
          <button
            type="button"
            aria-label="项目操作"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => {
              setMenuOpen((open) => !open);
              setConfirmDelete(false);
            }}
            className="inline-flex h-11 w-11 cursor-pointer items-center justify-center border border-[var(--border)] text-[var(--fg-1)] transition-colors hover:border-[var(--border-amber)] hover:text-[var(--amber-300)] md:h-8 md:w-8"
          >
            <MoreHorizontal className="h-4 w-4" />
          </button>
          {menuOpen ? (
            <div
              role="menu"
              className="absolute right-0 top-12 z-20 w-[min(18rem,calc(100vw-2rem))] max-w-[calc(100vw-2rem)] rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] p-1.5 shadow-[var(--shadow-2)]"
            >
              {confirmDelete ? (
                <div className="grid gap-2 p-2">
                  <p className="text-[15px] font-semibold tracking-tight text-[var(--fg-0)]">
                    确认删除这个项目？
                  </p>
                  <p className="text-xs leading-5 text-[var(--fg-2)]">
                    项目会从列表移除，关联对话不会被删除。
                  </p>
                  <div className="mt-1 flex justify-end gap-2">
                    <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmDelete(false)}>
                      取消
                    </Button>
                    <Button
                      type="button"
                      variant="danger"
                      size="sm"
                      loading={remove.isPending}
                      onClick={() => remove.mutate(workflow.id)}
                    >
                      删除
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="grid gap-0.5">
                  <button
                    type="button"
                    onClick={() => {
                      setTitle(workflowTitle);
                      setEditing(true);
                      setMenuOpen(false);
                    }}
                    role="menuitem"
                    className="flex min-h-11 cursor-pointer items-center gap-2.5 px-2 text-left text-[13px] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] md:min-h-9"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                    重命名
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmDelete(true)}
                    role="menuitem"
                    className="flex min-h-11 cursor-pointer items-center gap-2.5 px-2 text-left text-[13px] text-[var(--danger)] transition-colors hover:bg-[var(--danger-soft)] md:min-h-9"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                    删除
                  </button>
                </div>
              )}
            </div>
          ) : null}
        </div>
      </div>
    </header>
  );
}

function WorkflowStagePanel({ workflow }: { workflow: WorkflowRun }) {
  switch (workflow.current_step) {
    case "product_analysis":
      return <ProductAnalysisStage workflow={workflow} />;
    case "model_settings":
      return <ModelSettingsStage workflow={workflow} />;
    case "model_candidates":
    case "model_approval":
      return <ModelCandidatesStage workflow={workflow} />;
    case "showcase_generation":
      return <ShowcaseGenerationStage workflow={workflow} />;
    case "quality_review":
      return <QualityReviewStage workflow={workflow} />;
    case "delivery":
      return <DeliveryStage workflow={workflow} />;
    default:
      return <ProductUploadSummary workflow={workflow} />;
  }
}

function DetailSkeleton() {
  return (
    <div className="flex flex-1 items-center justify-center">
      <div className="grid place-items-center gap-3 text-center">
        <Spinner size={20} />
        <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-2)]">
          加载中
        </p>
      </div>
    </div>
  );
}

function DetailError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="m-6 max-w-md rounded-[var(--radius-card)] border border-[var(--danger)]/30 bg-[var(--danger-soft)]/20 p-5 text-sm">
      <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--danger)]">
        错误
      </p>
      <h3 className="type-card-title mt-1">
        项目加载失败
      </h3>
      <p className="mt-1 text-xs text-[var(--fg-1)]">
        网络错误或服务繁忙，请稍后重试。
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-4 inline-flex min-h-10 items-center gap-2 rounded-full border border-[var(--border)] px-4 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--fg-0)] transition-colors hover:border-[var(--border-amber)] hover:text-[var(--amber-300)]"
      >
        重试
      </button>
    </div>
  );
}
