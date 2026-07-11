"use client";

import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Check,
  Clapperboard,
  Film,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  Trash2,
  WandSparkles,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { useMemo, useState } from "react";

import type {
  StoryboardAsset,
  StoryboardRun,
  StoryboardShot,
} from "@/lib/apiClient";
import { useSSE } from "@/lib/useSSE";
import {
  qk,
  useApproveStoryboardAssetMutation,
  useApproveStoryboardKeyframeMutation,
  useApproveStoryboardShotMutation,
  useAssembleStoryboardMutation,
  useCreateStoryboardAssetMutation,
  useCreateStoryboardMutation,
  useCreateStoryboardShotMutation,
  useDeleteStoryboardAssetMutation,
  useDeleteStoryboardShotMutation,
  useGenerateAllStoryboardKeyframesMutation,
  useGenerateStoryboardAssetMutation,
  useGenerateStoryboardKeyframeMutation,
  useMoveStoryboardShotMutation,
  usePatchStoryboardMutation,
  usePatchStoryboardShotMutation,
  useRebuildStoryboardShotsMutation,
  useStoryboardQuery,
  useStoryboardsQuery,
  useSubmitAllStoryboardShotsMutation,
  useSubmitStoryboardShotMutation,
} from "@/lib/queries";
import { useQueryClient } from "@tanstack/react-query";
import { cn } from "@/lib/utils";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { OnlineBanner } from "../components/OnlineBanner";
import {
  ProjectMobileTabBar,
  ProjectMobileTopBar,
  ProjectTopBar,
} from "../components/ProjectTopBar";
import { formatRelativeTime } from "../utils";
import { StoryboardMediaFrame } from "./StoryboardMediaFrame";

type StoryboardStage =
  | "idea"
  | "script"
  | "assets"
  | "shots"
  | "keyframes"
  | "videos"
  | "assembly";

const STAGES: Array<{
  id: StoryboardStage;
  label: string;
  description: string;
}> = [
  { id: "idea", label: "想法", description: "项目名、想法和视觉风格" },
  { id: "script", label: "脚本", description: "脚本正文与锁定状态" },
  { id: "assets", label: "设定", description: "人物、场景、道具设定图" },
  { id: "shots", label: "分镜", description: "镜头拆分、顺序和绑定" },
  { id: "keyframes", label: "分镜图", description: "关键帧生成与审批" },
  { id: "videos", label: "视频", description: "逐镜头图生视频队列" },
  { id: "assembly", label: "成片", description: "合成、预览和下载" },
];

const STORYBOARD_SEED_MIN = -1;
const STORYBOARD_SEED_MAX = 4_294_967_295;

function parseStoryboardSeed(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) &&
    parsed >= STORYBOARD_SEED_MIN &&
    parsed <= STORYBOARD_SEED_MAX
    ? parsed
    : null;
}

const STATUS_TEXT: Record<string, string> = {
  draft: "草稿",
  in_progress: "进行中",
  completed: "完成",
  waiting_input: "待输入",
  generating: "生成中",
  ready: "待批准",
  approved: "已批准",
  keyframe_generating: "关键帧生成中",
  keyframe_ready: "关键帧待批准",
  keyframe_approved: "关键帧已批准",
  done: "完成",
  compositing: "合成中",
  failed: "失败",
};

function stageCompletion(run: StoryboardRun, stage: StoryboardStage): {
  done: boolean;
  active: boolean;
  count: string;
} {
  if (stage === "idea") {
    return { done: Boolean(run.idea.trim()), active: run.current_stage === stage, count: "" };
  }
  if (stage === "script") {
    return {
      done: run.script_confirmed,
      active: run.current_stage === stage,
      count: run.script_confirmed ? "已锁定" : run.script ? "待锁定" : "",
    };
  }
  if (stage === "assets") {
    const total = run.assets.length;
    const approved = run.assets.filter((asset) => asset.status === "approved").length;
    return {
      done: total > 0 && approved === total,
      active: run.current_stage === stage,
      count: total ? `${approved}/${total}` : "0",
    };
  }
  if (stage === "shots") {
    const total = run.shots.length;
    const approved = run.shots.filter((shot) => ["approved", "keyframe_generating", "keyframe_ready", "keyframe_approved", "generating", "done"].includes(shot.status)).length;
    return {
      done: total > 0 && approved === total,
      active: run.current_stage === stage,
      count: total ? `${approved}/${total}` : "0",
    };
  }
  if (stage === "keyframes") {
    const total = run.shots.length;
    const approved = run.shots.filter((shot) => shot.keyframe_approved_at && !shot.keyframe_stale).length;
    return {
      done: total > 0 && approved === total,
      active: run.current_stage === stage,
      count: total ? `${approved}/${total}` : "0",
    };
  }
  if (stage === "videos") {
    const total = run.shots.length;
    const done = run.shots.filter((shot) => shot.status === "done").length;
    return {
      done: total > 0 && done === total,
      active: run.current_stage === stage,
      count: total ? `${done}/${total}` : "0",
    };
  }
  return {
    done: run.assembly?.status === "done",
    active: run.current_stage === stage,
    count: run.assembly?.status ? STATUS_TEXT[run.assembly.status] ?? run.assembly.status : "",
  };
}

function isStageUnlocked(run: StoryboardRun, stage: StoryboardStage): boolean {
  if (stage === "idea") return true;
  if (stage === "script") return Boolean(run.idea.trim());
  if (stage === "assets") return run.script_confirmed;
  if (stage === "shots") return true;
  if (stage === "keyframes") return run.shots.length > 0;
  if (stage === "videos") {
    return run.shots.length > 0 && run.shots.every((shot) => Boolean(shot.keyframe_approved_at) && !shot.keyframe_stale);
  }
  return run.shots.length > 0 && run.shots.every((shot) => shot.status === "done");
}

function defaultStage(run: StoryboardRun): StoryboardStage {
  if (STAGES.some((stage) => stage.id === run.current_stage)) {
    return run.current_stage as StoryboardStage;
  }
  if (!run.script_confirmed) return "script";
  if (run.assets.length === 0) return "assets";
  if (run.shots.length === 0) return "shots";
  if (run.shots.some((shot) => !shot.keyframe_approved_at || shot.keyframe_stale)) return "keyframes";
  if (run.shots.some((shot) => shot.status !== "done")) return "videos";
  return "assembly";
}

export function StoryboardIndexPage() {
  const router = useRouter();
  const query = useStoryboardsQuery({ limit: 60 });
  const createMutation = useCreateStoryboardMutation({
    onSuccess: (run) => router.push(`/projects/storyboard/${run.id}`),
  });
  const [dialogOpen, setDialogOpen] = useState(false);
  const [title, setTitle] = useState("短视频分镜项目");
  const [idea, setIdea] = useState("");
  const [style, setStyle] = useState("");

  const submit = () => {
    if (!title.trim() || !idea.trim()) return;
    createMutation.mutate({
      title: title.trim(),
      idea: idea.trim(),
      style: style.trim(),
      aspect_ratio: "16:9",
      resolution: "720p",
      model: "seedance-2.0",
      generate_audio: true,
    });
  };

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <OnlineBanner />
      <ProjectMobileTopBar title="分镜制作" subtitle="项目列表" />
      <ProjectTopBar />

      <main className="lumen-studio-bg project-mobile-scroll mb-[calc(56px+env(safe-area-inset-bottom,0px))] min-h-0 flex-1 overflow-y-auto px-4 pt-2 md:mb-0 md:px-6 md:pb-6 md:pt-4">
        <div className="mx-auto grid w-full max-w-[1440px] gap-4">
          <div className="flex flex-wrap items-end justify-between gap-3 border-b border-[var(--border)] pb-4">
            <div className="min-w-0">
              <Link
                href="/projects"
                className="inline-flex items-center gap-1.5 text-xs font-medium text-[var(--fg-2)] transition-colors hover:text-[var(--fg-0)]"
              >
                <ArrowLeft className="h-3.5 w-3.5" />
                项目中心
              </Link>
              <h1 className="type-page-title mt-2">分镜制作</h1>
              <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--fg-1)]">
                每个项目都有独立状态、设定图、分镜、关键帧、视频段和成片合成；刷新页面后继续从服务端恢复。
              </p>
            </div>
            <button
              type="button"
              onClick={() => setDialogOpen(true)}
              className="inline-flex min-h-10 items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)] shadow-[var(--shadow-1)] transition hover:shadow-[var(--shadow-amber)]"
            >
              <Plus className="h-4 w-4" />
              新建项目
            </button>
          </div>

          {query.isLoading ? (
            <div className="grid min-h-64 place-items-center">
              <Spinner size={20} />
            </div>
          ) : query.isError ? (
            <button
              type="button"
              onClick={() => query.refetch()}
              className="min-h-40 border border-[var(--border)] bg-[var(--bg-1)] text-sm text-[var(--fg-1)] hover:bg-[var(--bg-2)]"
            >
              分镜项目加载失败，点击重试
            </button>
          ) : (query.data?.items ?? []).length === 0 ? (
            <div className="grid min-h-72 place-items-center border border-[var(--border)] bg-[var(--bg-1)]/72 p-6 text-center">
              <div className="max-w-sm">
                <Clapperboard className="mx-auto h-10 w-10 text-[var(--accent)]" />
                <h2 className="mt-3 text-lg font-semibold">还没有分镜项目</h2>
                <p className="mt-2 text-sm leading-6 text-[var(--fg-1)]">
                  从一个想法开始，后续脚本、设定、分镜图、视频段都会保存到项目里。
                </p>
                <button
                  type="button"
                  onClick={() => setDialogOpen(true)}
                  className="mt-4 inline-flex min-h-10 items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)]"
                >
                  <Plus className="h-4 w-4" />
                  新建项目
                </button>
              </div>
            </div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {(query.data?.items ?? []).map((item) => (
                <Link
                  key={item.id}
                  href={`/projects/storyboard/${item.id}`}
                  className="group grid min-h-56 gap-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/82 p-4 shadow-[var(--shadow-1)] transition hover:border-[var(--border-amber)] hover:shadow-[var(--shadow-2)]"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                        {STATUS_TEXT[item.status] ?? item.status}
                      </p>
                      <h2 className="mt-1 truncate text-lg font-semibold tracking-tight group-hover:text-[var(--accent)]">
                        {item.title}
                      </h2>
                    </div>
                    <ArrowRight className="h-4 w-4 shrink-0 text-[var(--fg-2)]" />
                  </div>
                  <StoryboardMediaFrame
                    src={item.thumbnail_url}
                    alt={`${item.title} 缩略图`}
                    className="h-28 w-full rounded-[var(--radius-card)] border border-[var(--border)]"
                    emptyClassName="grid h-28 place-items-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]"
                    emptyIcon={Film}
                    emptyIconClassName="h-7 w-7"
                  />
                  <p className="line-clamp-2 text-sm leading-6 text-[var(--fg-1)]">
                    {item.idea}
                  </p>
                  <div className="grid grid-cols-3 gap-2 text-xs text-[var(--fg-2)]">
                    <Metric label="设定" value={`${item.approved_asset_count}/${item.asset_count}`} />
                    <Metric label="镜头" value={String(item.shot_count)} />
                    <Metric label="完成" value={`${item.done_shot_count}/${item.shot_count}`} />
                  </div>
                  <p className="text-xs text-[var(--fg-3)]">
                    {formatRelativeTime(item.updated_at)}
                  </p>
                </Link>
              ))}
            </div>
          )}
        </div>
      </main>
      <ProjectMobileTabBar />

      {dialogOpen ? (
        <div className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-[var(--bg-0)]/70 backdrop-blur-sm sm:items-center sm:p-4">
          <section className="mobile-dialog-panel w-full max-w-xl rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] sm:rounded-[var(--radius-panel)] sm:border-b">
            <div className="border-b border-[var(--border)] p-4">
              <h2 className="text-base font-semibold">新建分镜项目</h2>
            </div>
            <div className="mobile-dialog-scroll grid gap-3 p-4">
              <LabeledInput label="项目名" value={title} onChange={setTitle} />
              <LabeledTextarea label="想法" value={idea} onChange={setIdea} rows={5} />
              <LabeledTextarea label="视觉风格" value={style} onChange={setStyle} rows={4} />
            </div>
            <footer className="mobile-dialog-footer flex justify-end gap-2 border-t border-[var(--border)] bg-[var(--bg-1)]/72 p-3">
              <button
                type="button"
                onClick={() => setDialogOpen(false)}
                className="min-h-10 rounded-[var(--radius-control)] border border-[var(--border)] px-4 text-sm text-[var(--fg-1)] hover:bg-[var(--bg-2)]"
              >
                取消
              </button>
              <button
                type="button"
                onClick={submit}
                disabled={!title.trim() || !idea.trim() || createMutation.isPending}
                className="inline-flex min-h-10 items-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)] disabled:opacity-60"
              >
                {createMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
                创建
              </button>
            </footer>
          </section>
        </div>
      ) : null}
    </div>
  );
}

export function StoryboardDetailPage({ storyboardId }: { storyboardId: string }) {
  const query = useStoryboardQuery(storyboardId);
  const qc = useQueryClient();
  const [selectedStage, setSelectedStage] = useState<StoryboardStage | null>(null);

  useSSE(
    [`storyboard:${storyboardId}`],
    useMemo(
      () => {
        const refresh = () => qc.invalidateQueries({ queryKey: qk.storyboard(storyboardId) });
        return {
          "storyboard.updated": refresh,
          "storyboard.asset_generating": refresh,
          "storyboard.asset_ready": refresh,
          "storyboard.keyframe_generating": refresh,
          "storyboard.keyframe_ready": refresh,
          "storyboard.shot_submitted": refresh,
          "storyboard.shot_done": refresh,
          "storyboard.assembling": refresh,
          "storyboard.assembled": refresh,
          "storyboard.assembly_failed": refresh,
          "generation.succeeded": refresh,
          "generation.failed": refresh,
          "generation.canceled": refresh,
          "video.progress": refresh,
          "video.fetching": refresh,
          "video.succeeded": refresh,
          "video.failed": refresh,
          "video.canceled": refresh,
        };
      },
      [qc, storyboardId],
    ),
  );

  const run = query.data;

  if (!run && query.isLoading) {
    return (
      <div className="grid h-[100dvh] place-items-center bg-[var(--bg-0)] text-[var(--fg-0)]">
        <Spinner size={20} />
      </div>
    );
  }

  if (!run) {
    return (
      <div className="grid h-[100dvh] place-items-center bg-[var(--bg-0)] p-6 text-center text-[var(--fg-0)]">
        <div>
          <p className="text-sm text-[var(--fg-1)]">分镜项目加载失败</p>
          <button
            type="button"
            onClick={() => query.refetch()}
            className="mt-3 min-h-10 rounded-[var(--radius-control)] border border-[var(--border)] px-4 text-sm hover:bg-[var(--bg-1)]"
          >
            重试
          </button>
        </div>
      </div>
    );
  }

  const activeStage = selectedStage ?? defaultStage(run);

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <OnlineBanner />
      <ProjectMobileTopBar title={run.title} subtitle="分镜工作区" />
      <ProjectTopBar />

      <main className="lumen-studio-bg mb-[calc(56px+env(safe-area-inset-bottom,0px))] grid min-h-0 flex-1 md:mb-0 md:grid-cols-[232px_minmax(0,1fr)_320px]">
        <StageRail run={run} activeStage={activeStage} onSelect={setSelectedStage} />
        <section className="min-h-0 overflow-y-auto border-x border-[var(--border)] px-4 py-3 md:px-5">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] pb-3">
            <div className="min-w-0">
              <Link href="/projects/storyboard" className="inline-flex items-center gap-1.5 text-xs text-[var(--fg-2)] hover:text-[var(--fg-0)]">
                <ArrowLeft className="h-3.5 w-3.5" />
                分镜项目
              </Link>
              <h1 className="mt-2 truncate text-xl font-semibold tracking-tight md:text-2xl">
                {run.title}
              </h1>
            </div>
            {query.isFetching ? (
              <span className="inline-flex items-center gap-2 text-xs text-[var(--fg-2)]">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                同步中
              </span>
            ) : null}
          </div>

          {activeStage === "idea" ? <IdeaStage run={run} /> : null}
          {activeStage === "script" ? <ScriptStage run={run} /> : null}
          {activeStage === "assets" ? <AssetsStage run={run} /> : null}
          {activeStage === "shots" ? <ShotsStage run={run} /> : null}
          {activeStage === "keyframes" ? <KeyframesStage run={run} /> : null}
          {activeStage === "videos" ? <VideosStage run={run} /> : null}
          {activeStage === "assembly" ? <AssemblyStage run={run} /> : null}
        </section>
        <SettingsPanel run={run} />
      </main>

      <ProjectMobileTabBar />
    </div>
  );
}

function StageRail({
  run,
  activeStage,
  onSelect,
}: {
  run: StoryboardRun;
  activeStage: StoryboardStage;
  onSelect: (stage: StoryboardStage) => void;
}) {
  return (
    <aside className="hidden min-h-0 overflow-y-auto p-3 md:block">
      <div className="grid gap-2">
        {STAGES.map((stage, index) => {
          const meta = stageCompletion(run, stage.id);
          const unlocked = isStageUnlocked(run, stage.id);
          const active = activeStage === stage.id;
          return (
            <button
              key={stage.id}
              type="button"
              onClick={() => unlocked && onSelect(stage.id)}
              className={cn(
                "grid min-h-[76px] gap-1 rounded-[var(--radius-card)] border p-3 text-left transition",
                active
                  ? "border-[var(--border-amber)] bg-[var(--accent-soft)]"
                  : "border-[var(--border)] bg-[var(--bg-1)]/74 hover:bg-[var(--bg-2)]",
                !unlocked && "cursor-not-allowed opacity-55",
              )}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span
                  className={cn(
                    "inline-flex h-5 min-w-5 items-center justify-center rounded-full border px-1 text-[10px]",
                    meta.done
                      ? "border-[var(--success-border)] bg-[var(--success-soft)] text-[var(--success-fg)]"
                      : "border-[var(--border)] text-[var(--fg-2)]",
                  )}
                >
                  {meta.done ? <Check className="h-3 w-3" /> : meta.count}
                </span>
              </span>
              <span className="text-sm font-semibold text-[var(--fg-0)]">{stage.label}</span>
              <span className="line-clamp-1 text-xs text-[var(--fg-2)]">{stage.description}</span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}

function SettingsPanel({ run }: { run: StoryboardRun }) {
  const patch = usePatchStoryboardMutation(run.id);
  const [model, setModel] = useState(run.model);
  const [resolution, setResolution] = useState(run.resolution);
  const [aspectRatio, setAspectRatio] = useState(run.aspect_ratio);
  const [generateAudio, setGenerateAudio] = useState(run.generate_audio);
  const [seed, setSeed] = useState(run.seed == null ? "" : String(run.seed));
  const parsedSeed = parseStoryboardSeed(seed);
  const seedInvalid = Boolean(seed.trim()) && parsedSeed === null;

  return (
    <aside className="hidden min-h-0 overflow-y-auto p-3 lg:block">
      <div className="grid gap-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3 shadow-[var(--shadow-1)]">
        <div className="flex items-center gap-2">
          <Settings2 className="h-4 w-4 text-[var(--accent)]" />
          <h2 className="text-sm font-semibold">视频参数</h2>
        </div>
        <LabeledInput label="模型" value={model} onChange={setModel} />
        <LabeledInput label="分辨率" value={resolution} onChange={setResolution} />
        <LabeledInput label="比例" value={aspectRatio} onChange={setAspectRatio} />
        <LabeledInput label="Seed" value={seed} onChange={setSeed} />
        {seedInvalid ? (
          <p className="text-xs text-[var(--danger)]" role="alert">
            Seed 需为 -1 到 4294967295 的整数
          </p>
        ) : null}
        <label className="flex min-h-10 items-center justify-between rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm">
          <span>生成音频</span>
          <input
            type="checkbox"
            checked={generateAudio}
            onChange={(event) => setGenerateAudio(event.target.checked)}
          />
        </label>
        <button
          type="button"
          disabled={patch.isPending || seedInvalid}
          onClick={() =>
            patch.mutate({
              model,
              resolution,
              aspect_ratio: aspectRatio,
              generate_audio: generateAudio,
              seed: parsedSeed,
            })
          }
          className="inline-flex min-h-10 items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-3 text-sm font-semibold text-[var(--accent-on)] disabled:cursor-not-allowed disabled:opacity-55"
        >
          {patch.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          保存参数
        </button>
      </div>
    </aside>
  );
}

function IdeaStage({ run }: { run: StoryboardRun }) {
  const patch = usePatchStoryboardMutation(run.id);
  const [title, setTitle] = useState(run.title);
  const [idea, setIdea] = useState(run.idea);
  const [style, setStyle] = useState(run.style);
  return (
    <StageShell title="想法" actionLabel="保存想法" loading={patch.isPending} onAction={() => patch.mutate({ title, idea, style, current_stage: "idea" })}>
      <div className="grid gap-3">
        <LabeledInput label="项目名" value={title} onChange={setTitle} />
        <LabeledTextarea label="想法" value={idea} onChange={setIdea} rows={7} />
        <LabeledTextarea label="视觉连续性" value={style} onChange={setStyle} rows={5} />
      </div>
    </StageShell>
  );
}

function ScriptStage({ run }: { run: StoryboardRun }) {
  const patch = usePatchStoryboardMutation(run.id);
  const [script, setScript] = useState(run.script);
  const scriptChanged = script !== run.script;
  return (
    <StageShell
      title="脚本"
      actionLabel={run.script_confirmed ? "更新脚本" : "保存并锁定脚本"}
      loading={patch.isPending}
      onAction={() =>
        patch.mutate({
          script,
          script_confirmed: run.script_confirmed && scriptChanged ? false : Boolean(script.trim()),
          current_stage: "script",
        })
      }
    >
      <div className="grid gap-3">
        <LabeledTextarea label="脚本正文" value={script} onChange={setScript} rows={14} />
        <InfoLine
          tone={run.script_confirmed ? "success" : "neutral"}
          text={run.script_confirmed ? "脚本已锁定，后续可以拆分分镜。" : "锁定脚本后会解锁设定阶段；修改脚本会进入待重新锁定状态。"}
        />
      </div>
    </StageShell>
  );
}

function AssetsStage({ run }: { run: StoryboardRun }) {
  const create = useCreateStoryboardAssetMutation(run.id);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"character" | "scene" | "prop">("character");
  const [description, setDescription] = useState("");
  return (
    <StageShell
      title="设定"
      actionLabel="新增设定"
      loading={create.isPending}
      onAction={() => {
        if (!name.trim()) return;
        create.mutate({ kind, name, description });
        setName("");
        setDescription("");
      }}
    >
      <div className="grid gap-4">
        <div className="grid gap-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/72 p-3 md:grid-cols-[160px_minmax(0,1fr)]">
          <label className="grid gap-1.5 text-sm">
            <span className="text-xs text-[var(--fg-2)]">类型</span>
            <select
              value={kind}
              onChange={(event) => setKind(event.target.value as "character" | "scene" | "prop")}
              className="min-h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-[var(--fg-0)]"
            >
              <option value="character">人物</option>
              <option value="scene">场景</option>
              <option value="prop">道具</option>
            </select>
          </label>
          <LabeledInput label="名称" value={name} onChange={setName} />
          <div className="md:col-span-2">
            <LabeledTextarea label="描述" value={description} onChange={setDescription} rows={3} />
          </div>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {run.assets.map((asset) => (
            <AssetCard key={asset.id} run={run} asset={asset} />
          ))}
        </div>
      </div>
    </StageShell>
  );
}

function AssetCard({ run, asset }: { run: StoryboardRun; asset: StoryboardAsset }) {
  const generate = useGenerateStoryboardAssetMutation(run.id, asset.id);
  const approve = useApproveStoryboardAssetMutation(run.id, asset.id);
  const remove = useDeleteStoryboardAssetMutation(run.id, asset.id);
  return (
    <article className="grid gap-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3 shadow-[var(--shadow-1)]">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs text-[var(--fg-2)]">{asset.kind}</p>
          <h3 className="truncate text-base font-semibold">{asset.name}</h3>
        </div>
        <StatusPill status={asset.status} />
      </div>
      <StoryboardMediaFrame
        src={asset.display_url || asset.image_url}
        alt={`${asset.name} 设定图`}
        className="aspect-video w-full rounded-[var(--radius-card)] border border-[var(--border)]"
        emptyClassName="grid aspect-video place-items-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]"
      />
      <p className="line-clamp-3 min-h-[4.5rem] text-sm leading-6 text-[var(--fg-1)]">
        {asset.description || "暂无描述"}
      </p>
      <InfoLine text="批准后将作为每个绑定分镜段的关键帧生成参考。" />
      <div className="grid grid-cols-3 gap-2">
        <IconAction icon={WandSparkles} label="生成" loading={generate.isPending} onClick={() => generate.mutate()} />
        <IconAction icon={Check} label="批准" disabled={!asset.image_id} loading={approve.isPending} onClick={() => approve.mutate()} />
        <IconAction icon={Trash2} label="删除" loading={remove.isPending} onClick={() => remove.mutate()} />
      </div>
    </article>
  );
}

function ShotsStage({ run }: { run: StoryboardRun }) {
  const rebuild = useRebuildStoryboardShotsMutation(run.id);
  const create = useCreateStoryboardShotMutation(run.id);
  return (
    <StageShell title="分镜" actionLabel="从脚本拆分" loading={rebuild.isPending} onAction={() => rebuild.mutate({ replace: true })}>
      <div className="grid gap-3">
        <button
          type="button"
          onClick={() => create.mutate({ title: `镜头 ${run.shots.length + 1}`, visual: "", duration_s: 5 })}
          className="inline-flex min-h-10 w-fit items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] px-3 text-sm hover:bg-[var(--bg-1)]"
        >
          <Plus className="h-4 w-4" />
          手动添加镜头
        </button>
        <div className="grid gap-3">
          {run.shots.map((shot) => (
            <ShotEditor key={shot.id} run={run} shot={shot} />
          ))}
        </div>
      </div>
    </StageShell>
  );
}

function ShotEditor({ run, shot }: { run: StoryboardRun; shot: StoryboardShot }) {
  const patch = usePatchStoryboardShotMutation(run.id, shot.id);
  const approve = useApproveStoryboardShotMutation(run.id, shot.id);
  const up = useMoveStoryboardShotMutation(run.id, shot.id);
  const remove = useDeleteStoryboardShotMutation(run.id, shot.id);
  const [title, setTitle] = useState(shot.title);
  const [visual, setVisual] = useState(shot.visual);
  const [narration, setNarration] = useState(shot.narration);
  const [assetIds, setAssetIds] = useState<string[]>(shot.asset_ids);

  return (
    <article className="grid gap-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/72 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
          SEG {String(shot.index).padStart(2, "0")}
        </span>
        <div className="flex flex-wrap gap-2">
          <StatusPill status={shot.status} />
          <IconAction icon={ArrowUp} label="上移" loading={up.isPending} onClick={() => up.mutate(-1)} />
          <IconAction icon={ArrowDown} label="下移" loading={up.isPending} onClick={() => up.mutate(1)} />
          <IconAction icon={Trash2} label="删除" loading={remove.isPending} onClick={() => remove.mutate()} />
        </div>
      </div>
      <LabeledInput label="镜头标题" value={title} onChange={setTitle} />
      <LabeledTextarea label="画面" value={visual} onChange={setVisual} rows={4} />
      <LabeledTextarea label="旁白/动作" value={narration} onChange={setNarration} rows={3} />
      <div className="flex flex-wrap gap-2">
        {run.assets.map((asset) => (
          <label
            key={asset.id}
            className={cn(
              "inline-flex min-h-8 items-center gap-2 rounded-full border px-3 text-xs",
              assetIds.includes(asset.id)
                ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--fg-0)]"
                : "border-[var(--border)] text-[var(--fg-1)]",
            )}
          >
            <input
              type="checkbox"
              checked={assetIds.includes(asset.id)}
              onChange={(event) =>
                setAssetIds((cur) =>
                  event.target.checked
                    ? [...cur, asset.id]
                    : cur.filter((id) => id !== asset.id),
                )
              }
            />
            {asset.name}
          </label>
        ))}
      </div>
      <InfoLine text="批准后才能生成该段的关键帧。" />
      <div className="flex flex-wrap gap-2">
        <IconAction icon={Save} label="保存" loading={patch.isPending} onClick={() => patch.mutate({ title, visual, narration, asset_ids: assetIds })} />
        <IconAction icon={Check} label="批准镜头" loading={approve.isPending} onClick={() => approve.mutate()} />
      </div>
    </article>
  );
}

function KeyframesStage({ run }: { run: StoryboardRun }) {
  const generateAll = useGenerateAllStoryboardKeyframesMutation(run.id);
  return (
    <StageShell title="分镜图" actionLabel="批量生成未完成关键帧" loading={generateAll.isPending} onAction={() => generateAll.mutate()}>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {run.shots.map((shot) => (
          <KeyframeCard key={shot.id} run={run} shot={shot} />
        ))}
      </div>
    </StageShell>
  );
}

function KeyframeCard({ run, shot }: { run: StoryboardRun; shot: StoryboardShot }) {
  const generate = useGenerateStoryboardKeyframeMutation(run.id, shot.id);
  const approve = useApproveStoryboardKeyframeMutation(run.id, shot.id);
  return (
    <article className="grid gap-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3">
      {shot.keyframe_stale ? (
        <div className="rounded-[var(--radius-control)] border border-[var(--warning-border,var(--border))] bg-[var(--warning-soft,var(--bg-2))] px-3 py-2 text-xs text-[var(--warning-fg,var(--fg-0))]">
          绑定的设定图已更新，关键帧需要重新生成。
        </div>
      ) : null}
      <div className="flex items-center justify-between gap-2">
        <h3 className="truncate text-sm font-semibold">{shot.title}</h3>
        <StatusPill status={shot.status} />
      </div>
      <StoryboardMediaFrame
        src={shot.keyframe_display_url || shot.keyframe_image_url}
        alt={`${shot.title} 关键帧`}
        className="aspect-video w-full rounded-[var(--radius-card)] border border-[var(--border)]"
        emptyClassName="grid aspect-video place-items-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]"
      />
      <InfoLine text="批准后才能提交该段视频生成，修改关键帧会使批准失效。" />
      <div className="grid grid-cols-2 gap-2">
        <IconAction icon={RefreshCw} label={shot.keyframe_stale ? "重新生成" : "生成"} loading={generate.isPending} onClick={() => generate.mutate()} />
        <IconAction icon={Check} label="批准" disabled={!shot.keyframe_image_id || shot.keyframe_stale} loading={approve.isPending} onClick={() => approve.mutate()} />
      </div>
    </article>
  );
}

function VideosStage({ run }: { run: StoryboardRun }) {
  const submitAll = useSubmitAllStoryboardShotsMutation(run.id);
  return (
    <StageShell title="视频" actionLabel="全部提交" loading={submitAll.isPending} onAction={() => submitAll.mutate()}>
      <div className="grid gap-2">
        {run.shots.map((shot) => (
          <VideoQueueRow key={shot.id} run={run} shot={shot} />
        ))}
      </div>
    </StageShell>
  );
}

function VideoQueueRow({ run, shot }: { run: StoryboardRun; shot: StoryboardShot }) {
  const submit = useSubmitStoryboardShotMutation(run.id, shot.id);
  const pct = shot.video_progress_pct ?? (shot.status === "done" ? 100 : 0);
  const canSubmitVideo =
    shot.status === "keyframe_approved" &&
    Boolean(shot.keyframe_image_id) &&
    !shot.keyframe_stale;
  return (
    <article className="grid gap-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/74 p-3 md:grid-cols-[88px_minmax(0,1fr)_auto] md:items-center">
      <StoryboardMediaFrame
        src={shot.keyframe_display_url || shot.keyframe_image_url}
        alt={`${shot.title} 视频参考帧`}
        className="aspect-video w-full rounded-[var(--radius-control)] border border-[var(--border)] md:w-20"
        emptyClassName="grid aspect-video place-items-center rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] md:w-20"
        emptyIconClassName="h-5 w-5 text-[var(--fg-2)]"
        sizes="80px"
      />
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
            SEG {String(shot.index).padStart(2, "0")}
          </span>
          <StatusPill status={shot.video_status || shot.status} />
        </div>
        <h3 className="mt-1 truncate text-sm font-semibold">{shot.title}</h3>
        <div className="mt-2 h-2 overflow-hidden rounded-full bg-[var(--bg-2)]">
          <div className="h-full bg-[var(--accent)] transition-all" style={{ width: `${Math.max(0, Math.min(100, pct))}%` }} />
        </div>
      </div>
      <div className="flex gap-2">
        {shot.video?.url ? (
          <a href={shot.video.url} target="_blank" rel="noreferrer" className="inline-flex min-h-9 items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] px-3 text-xs hover:bg-[var(--bg-2)]">
            <Play className="h-3.5 w-3.5" />
            预览
          </a>
        ) : null}
        <IconAction icon={Film} label="提交" disabled={!canSubmitVideo} loading={submit.isPending} onClick={() => submit.mutate()} />
      </div>
    </article>
  );
}

function AssemblyStage({ run }: { run: StoryboardRun }) {
  const assemble = useAssembleStoryboardMutation(run.id);
  const ready = run.shots.length > 0 && run.shots.every((shot) => shot.status === "done");
  return (
    <StageShell title="成片" actionLabel="合成成片" loading={assemble.isPending} disabled={!ready} onAction={() => assemble.mutate()}>
      <div className="grid gap-4 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/74 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-sm font-semibold">合成状态</p>
            <p className="mt-1 text-sm text-[var(--fg-1)]">
              {STATUS_TEXT[run.assembly?.status || "waiting_input"] ?? run.assembly?.status ?? "等待视频段完成"}
            </p>
          </div>
          <StatusPill status={run.assembly?.status || "waiting_input"} />
        </div>
        {run.assembly?.video_url ? (
          <video src={run.assembly.video_url} poster={run.assembly.poster_url || undefined} controls className="max-h-[62vh] w-full rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]" />
        ) : (
          <div className="grid min-h-52 place-items-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] text-center text-[var(--fg-2)]">
            {ready ? "所有片段已完成，可以合成成片。" : "所有视频段完成后才能合成成片。"}
          </div>
        )}
        {run.assembly?.video_url ? (
          <a href={run.assembly.video_url} download className="inline-flex min-h-10 w-fit items-center justify-center rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)]">
            下载 mp4
          </a>
        ) : null}
      </div>
    </StageShell>
  );
}

function StageShell({
  title,
  children,
  actionLabel,
  loading,
  disabled,
  onAction,
}: {
  title: string;
  children: ReactNode;
  actionLabel: string;
  loading?: boolean;
  disabled?: boolean;
  onAction: () => void;
}) {
  return (
    <section className="grid gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="type-section-title">{title}</h2>
        <button
          type="button"
          onClick={onAction}
          disabled={disabled || loading}
          className="inline-flex min-h-10 items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)] shadow-[var(--shadow-1)] disabled:opacity-60"
        >
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          {actionLabel}
        </button>
      </div>
      {children}
    </section>
  );
}

function LabeledInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grid gap-1.5 text-sm">
      <span className="text-xs font-medium text-[var(--fg-2)]">{label}</span>
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="min-h-10 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-[var(--fg-0)] outline-none transition focus:border-[var(--border-strong)]"
      />
    </label>
  );
}

function LabeledTextarea({
  label,
  value,
  onChange,
  rows,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  rows: number;
}) {
  return (
    <label className="grid gap-1.5 text-sm">
      <span className="text-xs font-medium text-[var(--fg-2)]">{label}</span>
      <textarea
        value={value}
        rows={rows}
        onChange={(event) => onChange(event.target.value)}
        className="resize-y rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2 text-[var(--fg-0)] outline-none transition focus:border-[var(--border-strong)]"
      />
    </label>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2">
      <p className="text-[10px] text-[var(--fg-2)]">{label}</p>
      <p className="mt-0.5 font-mono text-xs text-[var(--fg-0)]">{value}</p>
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const success = ["approved", "keyframe_approved", "done", "completed"].includes(status);
  const busy = [
    "generating",
    "keyframe_generating",
    "compositing",
    "running",
    "queued",
    "submitting",
    "submit_unknown",
    "submitted",
  ].includes(status);
  return (
    <span
      className={cn(
        "inline-flex min-h-6 items-center gap-1 rounded-full border px-2 text-[11px] font-medium",
        success
          ? "border-[var(--success-border)] bg-[var(--success-soft)] text-[var(--success-fg)]"
          : busy
            ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]"
            : "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-1)]",
      )}
    >
      {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
      {STATUS_TEXT[status] ?? status}
    </span>
  );
}

function InfoLine({ text, tone = "neutral" }: { text: string; tone?: "neutral" | "success" }) {
  return (
    <p
      className={cn(
        "rounded-[var(--radius-control)] border px-3 py-2 text-xs leading-5",
        tone === "success"
          ? "border-[var(--success-border)] bg-[var(--success-soft)] text-[var(--success-fg)]"
          : "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]",
      )}
    >
      {text}
    </p>
  );
}

function IconAction({
  icon: Icon,
  label,
  loading,
  disabled,
  onClick,
}: {
  icon: typeof Save;
  label: string;
  loading?: boolean;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || loading}
      className="inline-flex min-h-9 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-xs font-medium text-[var(--fg-0)] transition hover:bg-[var(--bg-2)] disabled:opacity-55"
    >
      {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Icon className="h-3.5 w-3.5" />}
      {label}
    </button>
  );
}
