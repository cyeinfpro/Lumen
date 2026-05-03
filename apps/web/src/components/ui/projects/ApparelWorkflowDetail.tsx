"use client";

// 详情页 / 控制台：
// 1) 三栏（StepRail | StagePanel | ConstraintPanel），中屏改抽屉
// 2) AnimatePresence 阶段切换淡入；StageErrorBoundary 兜底子组件 crash
// 3) 顶部就地编辑标题；更多菜单提供删除项目
// 4) 标题区右侧显示 last updated；refreshing 时加 spinner 提示
// 5) keyboard：⌘/Ctrl + . 切换右侧约束面板抽屉

import { AnimatePresence, motion } from "framer-motion";
import { ArrowLeft, Check, ChevronDown, Loader2, MessageSquare, MoreVertical, PanelRightOpen, Pencil, Trash2, X } from "lucide-react";
import Link from "next/link";
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
import { STATUS_LABEL } from "./types";
import { formatRelativeTime } from "./utils";

interface DetailProps {
  projectId: string;
}

export function ApparelWorkflowDetail({ projectId }: DetailProps) {
  const query = useWorkflowQuery(projectId);
  const workflow = query.data;

  return (
    <div className="relative flex h-[100dvh] w-full min-w-0 flex-col bg-[var(--bg-0)]">
      <div data-topbar-sentinel className="absolute top-0 h-1 w-full" aria-hidden />
      <OnlineBanner />
      <ProjectMobileTopBar
        title="项目"
        subtitle={workflow ? STATUS_LABEL[workflow.status] ?? workflow.status : "加载中"}
        backHref="/projects"
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

  // 快捷键 ⌘/Ctrl + . 切换右侧抽屉（仅中屏以下）
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
    <main className="mb-[calc(56px+env(safe-area-inset-bottom,0px))] grid flex-1 overflow-hidden md:mb-0 lg:grid-cols-[240px_minmax(0,1fr)] xl:grid-cols-[240px_minmax(0,1fr)_320px]">
      <aside className="hidden border-r border-[var(--border)] bg-white/[0.025] p-4 lg:block">
        <StepRail workflow={workflow} />
      </aside>

      <section className="min-w-0 overflow-y-auto p-4 md:p-6">
        <DetailBreadcrumb workflow={workflow} />
        <DetailHeader workflow={workflow} refreshing={refreshing} onOpenDrawer={() => setDrawerOpen(true)} />
        <MobileStageStrip workflow={workflow} />

        <StageErrorBoundary resetKeys={[workflow.id, workflow.current_step]}>
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={workflow.current_step}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
            >
              <WorkflowStagePanel workflow={workflow} />
            </motion.div>
          </AnimatePresence>
        </StageErrorBoundary>

        <Conversation workflow={workflow} />
      </section>

      <aside className="hidden overflow-y-auto border-l border-[var(--border)] bg-white/[0.025] p-4 xl:block">
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

function DetailBreadcrumb({ workflow }: { workflow: WorkflowRun }) {
  return (
    <nav aria-label="项目路径" className="mb-4 hidden min-w-0 items-center gap-1.5 text-sm md:flex">
      <Link
        href="/projects"
        className="inline-flex items-center gap-1.5 text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)]"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        项目
      </Link>
      <span aria-hidden className="text-[var(--fg-3)]">/</span>
      <span className="min-w-0 truncate text-[var(--fg-0)]">
        {workflow.title || "服饰模特展示图"}
      </span>
    </nav>
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
  const workflowTitle = workflow.title || "服饰模特展示图";
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(workflowTitle);
  const trackedWorkflowTitleRef = useRef(workflowTitle);
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const patch = usePatchWorkflowMutation({
    onSuccess: (data) => {
      setTitle(data.title || "服饰模特展示图");
      setEditing(false);
      toast.success("项目已重命名");
    },
    onError: (error) => toast.error(error.message || "重命名失败"),
  });
  const remove = useDeleteWorkflowMutation({
    onSuccess: () => {
      toast.success("项目已删除");
      router.push("/projects");
    },
    onError: (error) => toast.error(error.message || "删除失败"),
  });
  // 上游 workflow.title 变化时把本地草稿同步过去；ref 替代之前 render 阶段
  // setState 的 anti-pattern，React 19 strict mode 下会抛 warning。
  // editing 时用户正在改，跳过覆盖，避免吞掉用户输入。
  useEffect(() => {
    if (editing) return;
    if (trackedWorkflowTitleRef.current === workflowTitle) return;
    trackedWorkflowTitleRef.current = workflowTitle;
    setTitle(workflowTitle);
  }, [editing, workflowTitle]);

  // menu 点外面关 + Esc 关
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
  const statusTone = useMemo(() => {
    if (status === "completed")
      return "border-[var(--success)]/30 bg-[var(--success-soft)] text-[var(--success)]";
    if (status === "running" || status === "needs_review")
      return "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]";
    if (status === "failed")
      return "border-[var(--danger)]/30 bg-[var(--danger-soft)] text-[var(--danger)]";
    return "border-[var(--border)] bg-white/[0.04] text-[var(--fg-1)]";
  }, [status]);

  return (
    <header className="mb-4 rounded-xl border border-[var(--border)] bg-[var(--bg-1)]/70 p-3 shadow-[var(--shadow-1)] md:flex md:flex-wrap md:items-start md:justify-between md:gap-3 md:rounded-none md:border-0 md:bg-transparent md:p-0 md:shadow-none">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          {editing ? (
            <form
              className="flex min-w-0 items-center gap-1.5"
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
                className="h-11 min-w-0 max-w-[min(70vw,520px)] rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-[18px] font-semibold text-[var(--fg-0)] outline-none focus:border-[var(--border-amber)] md:h-10 md:text-[20px]"
              />
              <button
                type="submit"
                aria-label="保存项目名称"
                disabled={patch.isPending}
                className="inline-flex h-10 w-10 cursor-pointer items-center justify-center rounded-md text-[var(--fg-1)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)] disabled:opacity-50 md:h-9 md:w-9"
              >
                {patch.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
              </button>
              <button
                type="button"
                aria-label="取消重命名"
                onClick={() => setEditing(false)}
                className="inline-flex h-10 w-10 cursor-pointer items-center justify-center rounded-md text-[var(--fg-2)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)] md:h-9 md:w-9"
              >
                <X className="h-4 w-4" />
              </button>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="group/title flex min-w-0 cursor-pointer items-center gap-2 rounded-md text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
            >
              <h1 className="line-clamp-2 text-[22px] font-semibold leading-[1.18] tracking-normal text-[var(--fg-0)] md:truncate">
                {workflow.title || "服饰模特展示图"}
              </h1>
              <Pencil className="h-4 w-4 shrink-0 text-[var(--fg-3)] opacity-0 transition-opacity group-hover/title:opacity-100" />
            </button>
          )}
          <span
            className={cn(
              "inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[10px]",
              statusTone,
            )}
          >
            {status === "running" || status === "needs_review" ? (
              <span className="h-1.5 w-1.5 rounded-full bg-current animate-[lumen-pulse-soft_1800ms_ease-in-out_infinite]" />
            ) : null}
            {STATUS_LABEL[status] ?? status}
          </span>
        </div>
        <p className="mt-1 line-clamp-2 max-w-2xl text-sm text-[var(--fg-2)]">
          {workflow.user_prompt || "未填写基础需求"}
        </p>
        <p className="mt-1 text-xs text-[var(--fg-3)]">
          更新于 {formatRelativeTime(workflow.updated_at)}
        </p>
      </div>
      <div className="mt-3 flex items-center justify-between gap-2 md:mt-0 md:justify-start">
        {refreshing ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-[var(--fg-2)]">
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            同步中
          </span>
        ) : null}
        <button
          type="button"
          onClick={onOpenDrawer}
          aria-label="查看项目约束 (⌘ .)"
          className="inline-flex min-h-10 cursor-pointer items-center gap-1.5 rounded-md border border-[var(--border)] bg-white/[0.04] px-3 text-xs text-[var(--fg-0)] transition-colors hover:bg-white/[0.08] xl:hidden"
        >
          <PanelRightOpen className="h-3.5 w-3.5" />
          约束
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
            className="inline-flex h-10 w-10 cursor-pointer items-center justify-center rounded-md border border-[var(--border)] bg-white/[0.04] text-[var(--fg-1)] transition-colors hover:bg-white/[0.08] hover:text-[var(--fg-0)] md:h-9 md:w-9"
          >
            <MoreVertical className="h-4 w-4" />
          </button>
          {menuOpen ? (
            <div role="menu" className="absolute right-0 top-12 z-20 w-[min(18rem,calc(100vw-2rem))] rounded-lg border border-[var(--border)] bg-[var(--bg-1)] p-2 shadow-[var(--shadow-2)] md:top-11 md:w-64 md:rounded-md">
              {confirmDelete ? (
                <div className="grid gap-2">
                  <p className="text-sm text-[var(--fg-0)]">确认删除这个项目？</p>
                  <p className="text-xs leading-5 text-[var(--fg-2)]">项目会从列表移除，关联对话不会被删除。</p>
                  <div className="flex justify-end gap-2">
                    <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmDelete(false)}>
                      取消
                    </Button>
                    <Button type="button" variant="danger" size="sm" loading={remove.isPending} onClick={() => remove.mutate(workflow.id)}>
                      删除
                    </Button>
                  </div>
                </div>
              ) : (
                <div className="grid gap-1">
                  <button
                    type="button"
                    onClick={() => {
                      setEditing(true);
                      setMenuOpen(false);
                    }}
                    role="menuitem"
                    className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md px-2 text-left text-sm text-[var(--fg-1)] transition-colors hover:bg-white/[0.06] hover:text-[var(--fg-0)] md:min-h-9"
                  >
                    <Pencil className="h-4 w-4" />
                    重命名
                  </button>
                  <button
                    type="button"
                    onClick={() => setConfirmDelete(true)}
                    role="menuitem"
                    className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md px-2 text-left text-sm text-[var(--danger)] transition-colors hover:bg-[var(--danger-soft)] md:min-h-9"
                  >
                    <Trash2 className="h-4 w-4" />
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
    case "quality_review":
    case "delivery":
      return <ShowcaseGenerationStage workflow={workflow} />;
    default:
      return <ProductUploadSummary workflow={workflow} />;
  }
}

function Conversation({ workflow }: { workflow: WorkflowRun }) {
  const [open, setOpen] = useState(false);
  if (!workflow.conversation_id) return null;
  return (
    <section className="mt-5 rounded-md border border-[var(--border)] bg-white/[0.025] p-3">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between gap-2 rounded-md px-1 py-1 text-left text-sm text-[var(--fg-1)] transition-colors hover:text-[var(--fg-0)]"
      >
        <span className="inline-flex items-center gap-2">
          <MessageSquare className="h-4 w-4 text-[var(--fg-2)]" />
          对话上下文
        </span>
        <ChevronDown
          className={cn("h-4 w-4 transition-transform", open && "rotate-180")}
          aria-hidden
        />
      </button>
      {open ? (
        <div className="mt-2 rounded-md border border-[var(--border)] bg-[var(--bg-2)]/40 p-3 text-xs leading-6 text-[var(--fg-2)]">
          <Link
            href={`/?conversationId=${workflow.conversation_id}`}
            className="text-[var(--amber-300)] hover:underline underline-offset-2"
          >
            打开关联对话
          </Link>
          <p className="mt-1">所有阶段的派发与确认都会写入这条对话的事件流，可在创作页继续追问与微调。</p>
        </div>
      ) : null}
    </section>
  );
}

function DetailSkeleton() {
  return (
    <div className="flex flex-1 items-center justify-center">
      <Spinner size={20} />
    </div>
  );
}

function DetailError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="m-6 max-w-md rounded-md border border-[var(--danger)]/30 bg-[var(--danger-soft)] p-5 text-sm">
      <h3 className="text-base font-medium text-[var(--fg-0)]">项目加载失败</h3>
      <p className="mt-1 text-xs text-[var(--fg-1)]">网络错误或服务繁忙，请稍后重试。</p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 inline-flex h-9 items-center gap-1.5 rounded-md border border-[var(--border)] bg-white/[0.04] px-3 text-xs text-[var(--fg-0)] transition-colors hover:bg-white/[0.08]"
      >
        重试
      </button>
    </div>
  );
}
