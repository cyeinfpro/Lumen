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
  Plus,
  RefreshCw,
  Save,
  Settings2,
  Trash2,
  WandSparkles,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useRef, useState } from "react";

import { useModalLayer } from "@/components/ui/primitives/mobile/useModalLayer";
import type {
  StoryboardAsset,
  StoryboardRun,
  StoryboardShot,
} from "@/lib/apiClient";
import { useUserQueryScope } from "@/components/QueryProvider";
import { useSSE } from "@/features/realtime";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import {
  qk,
  useApproveStoryboardAssetMutation,
  useApproveStoryboardKeyframeMutation,
  useApproveStoryboardShotMutation,
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
} from "@/lib/queries";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/primitives/Button";
import { Select } from "@/components/ui/primitives/Select";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { OnlineBanner } from "../components/OnlineBanner";
import {
  ProjectMobileTabBar,
  ProjectMobileTopBar,
  ProjectTopBar,
} from "../components/ProjectTopBar";
import { formatRelativeTime } from "../utils";
import { StoryboardMediaFrame } from "./StoryboardMediaFrame";
import {
  defaultStage,
  isStageUnlocked,
  parseStoryboardStage,
  type StoryboardStage,
} from "./StoryboardDomain";
import { SettingsPanel } from "./StoryboardSettingsPanel";
import { StageRail } from "./StoryboardStageRail";
import { AssemblyStage, VideosStage } from "./StoryboardVideoStages";
import {
  IconAction,
  InfoLine,
  LabeledInput,
  LabeledTextarea,
  Metric,
  notifyStoryboardError,
  StageShell,
  StatusPill,
  STATUS_TEXT,
} from "./StoryboardShared";

export function StoryboardIndexPage() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const search = searchParams.toString();
  const query = useStoryboardsQuery({ limit: 60 });
  const createMutation = useCreateStoryboardMutation({
    onSuccess: (run) => router.push(`/projects/storyboard/${run.id}`),
    onError: notifyStoryboardError("创建分镜项目"),
  });
  const [title, setTitle] = useState("短视频分镜项目");
  const [idea, setIdea] = useState("");
  const [style, setStyle] = useState("");
  const dialogRef = useRef<HTMLElement | null>(null);
  const dialogOpen = searchParams.get("new") === "1";

  const setDialogOpen = useCallback(
    (open: boolean) => {
      const next = new URLSearchParams(search);
      if (open) next.set("new", "1");
      else next.delete("new");
      const queryString = next.toString();
      router.replace(`${pathname}${queryString ? `?${queryString}` : ""}`, {
        scroll: false,
      });
    },
    [pathname, router, search],
  );
  const closeDialog = useCallback(() => {
    if (createMutation.isPending) return;
    setTitle("短视频分镜项目");
    setIdea("");
    setStyle("");
    setDialogOpen(false);
  }, [createMutation.isPending, setDialogOpen]);
  useBodyScrollLock(dialogOpen);
  const onDialogKeyDown = useModalLayer({
    open: dialogOpen,
    rootRef: dialogRef,
    onClose: closeDialog,
  });

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

      <main className="lumen-studio-bg project-mobile-scroll mb-[var(--mobile-tabbar-height)] min-h-0 flex-1 overflow-y-auto px-3 pt-2 min-[390px]:px-4 md:mb-0 md:px-6 md:pb-6 md:pt-4">
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
              className="inline-flex min-h-11 items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)] shadow-[var(--shadow-1)] transition hover:shadow-[var(--shadow-amber)] sm:min-h-10"
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
                  className="mt-4 inline-flex min-h-11 items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)] sm:min-h-10"
                >
                  <Plus className="h-4 w-4" />
                  新建项目
                </button>
              </div>
            </div>
          ) : (
            <div className="grid gap-3 min-[390px]:grid-cols-2 md:grid-cols-2 xl:grid-cols-3">
              {(query.data?.items ?? []).map((item) => (
                <Link
                  key={item.id}
                  href={`/projects/storyboard/${item.id}`}
                  className="group grid min-h-56 gap-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/82 p-4 shadow-[var(--shadow-1)] transition hover:border-accent-border hover:shadow-[var(--shadow-2)]"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="type-caption text-[var(--fg-3)]">
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
        <div
          className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-[var(--bg-0)]/70 backdrop-blur-sm sm:items-center sm:p-4"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeDialog();
          }}
        >
          <section
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="storyboard-create-title"
            tabIndex={-1}
            onKeyDown={onDialogKeyDown}
            className="mobile-dialog-panel w-full max-w-xl rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] focus-visible:outline-none sm:rounded-[var(--radius-panel)] sm:border-b"
          >
            <div className="border-b border-[var(--border)] p-4">
              <h2 id="storyboard-create-title" className="text-base font-semibold">
                新建分镜项目
              </h2>
            </div>
            <div className="mobile-dialog-scroll grid gap-3 p-4">
              <LabeledInput label="项目名" value={title} onChange={setTitle} />
              <LabeledTextarea label="想法" value={idea} onChange={setIdea} rows={5} />
              <LabeledTextarea label="视觉风格" value={style} onChange={setStyle} rows={4} />
            </div>
            <footer className="mobile-dialog-footer grid grid-cols-1 gap-2 border-t border-[var(--border)] bg-[var(--bg-1)]/72 p-3 min-[390px]:flex min-[390px]:justify-end">
              <button
                type="button"
                onClick={closeDialog}
                disabled={createMutation.isPending}
                className="min-h-11 rounded-[var(--radius-control)] border border-[var(--border)] px-4 text-sm text-[var(--fg-1)] hover:bg-[var(--bg-2)] min-[390px]:min-h-10"
              >
                取消
              </button>
              <button
                type="button"
                onClick={submit}
                disabled={!title.trim() || !idea.trim() || createMutation.isPending}
                className="inline-flex min-h-11 items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--accent)] px-4 text-sm font-semibold text-[var(--accent-on)] disabled:opacity-60 min-[390px]:min-h-10"
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
  const userScope = useUserQueryScope();
  const qc = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const search = searchParams.toString();
  const requestedStage = parseStoryboardStage(searchParams.get("stage"));
  const [settingsOpen, setSettingsOpen] = useState(false);

  useSSE(
    [`storyboard:${storyboardId}`],
    useMemo(
      () => {
        const userKeys = qk.user(userScope.userId);
        const refreshDetail = () => {
          qc.invalidateQueries({ queryKey: userKeys.storyboard(storyboardId) });
        };
        const refreshDetailAndList = () => {
          refreshDetail();
          qc.invalidateQueries({ queryKey: userKeys.storyboardsAll() });
        };
        return {
          "storyboard.updated": refreshDetailAndList,
          "storyboard.deleted": refreshDetailAndList,
          "storyboard.asset_generating": refreshDetailAndList,
          "storyboard.asset_ready": refreshDetailAndList,
          "storyboard.keyframe_generating": refreshDetailAndList,
          "storyboard.keyframe_ready": refreshDetailAndList,
          "storyboard.shot_submitted": refreshDetailAndList,
          "storyboard.shot_done": refreshDetailAndList,
          "storyboard.assembling": refreshDetailAndList,
          "storyboard.assembled": refreshDetailAndList,
          "storyboard.assembly_failed": refreshDetailAndList,
          "generation.succeeded": refreshDetailAndList,
          "generation.failed": refreshDetailAndList,
          "generation.canceled": refreshDetailAndList,
          "video.progress": refreshDetail,
          "video.fetching": refreshDetailAndList,
          "video.succeeded": refreshDetailAndList,
          "video.failed": refreshDetailAndList,
          "video.canceled": refreshDetailAndList,
        };
      },
      [qc, storyboardId, userScope.userId],
    ),
  );

  const run = query.data;
  const selectStage = useCallback(
    (stage: StoryboardStage) => {
      const current = query.data;
      if (!current || !isStageUnlocked(current, stage)) return;
      const next = new URLSearchParams(search);
      next.set("stage", stage);
      router.replace(`${pathname}?${next.toString()}`, { scroll: false });
    },
    [pathname, query.data, router, search],
  );

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

  const activeStage =
    requestedStage && isStageUnlocked(run, requestedStage)
      ? requestedStage
      : defaultStage(run);

  return (
    <div className="relative flex h-[100dvh] min-h-0 w-full min-w-0 flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      <OnlineBanner />
      <ProjectMobileTopBar
        title={run.title}
        subtitle="分镜工作区"
        right={
          <button
            type="button"
            onClick={() => setSettingsOpen(true)}
            aria-label="视频参数"
            className="inline-flex h-11 w-11 items-center justify-center rounded-full border border-[var(--border)] text-[var(--fg-1)]"
          >
            <Settings2 className="h-4 w-4" />
          </button>
        }
      />
      <ProjectTopBar />

      <main className="lumen-studio-bg mb-[var(--mobile-tabbar-height)] flex min-h-0 flex-1 flex-col md:mb-0 md:grid md:grid-cols-[232px_minmax(0,1fr)] lg:grid-cols-[232px_minmax(0,1fr)_320px]">
        <StageRail run={run} activeStage={activeStage} onSelect={selectStage} />
        <section className="min-h-0 flex-1 overflow-y-auto border-[var(--border)] px-3 py-3 min-[390px]:px-4 md:border-y-0 md:border-x md:px-5">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] pb-3">
            <div className="min-w-0">
              <Link href="/projects/storyboard" className="inline-flex items-center gap-1.5 text-xs text-[var(--fg-2)] hover:text-[var(--fg-0)]">
                <ArrowLeft className="h-3.5 w-3.5" />
                分镜项目
              </Link>
              <h1 className="mt-2 break-words text-xl font-semibold tracking-tight md:truncate md:text-2xl">
                {run.title}
              </h1>
            </div>
            <div className="flex items-center gap-2">
              {query.isFetching ? (
                <span className="inline-flex items-center gap-2 text-xs text-[var(--fg-2)]">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  同步中
                </span>
              ) : null}
              <button
                type="button"
                onClick={() => setSettingsOpen(true)}
                className="hidden min-h-11 items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] px-3 text-xs text-[var(--fg-1)] md:inline-flex lg:hidden"
              >
                <Settings2 className="h-3.5 w-3.5" />
                视频参数
              </button>
            </div>
          </div>

          {activeStage === "idea" ? (
            <IdeaStage
              key={[run.id, run.title, run.idea, run.style].join(":")}
              run={run}
            />
          ) : null}
          {activeStage === "script" ? (
            <ScriptStage
              key={[run.id, run.script_revision, run.script_confirmed].join(":")}
              run={run}
            />
          ) : null}
          {activeStage === "assets" ? <AssetsStage run={run} /> : null}
          {activeStage === "shots" ? <ShotsStage run={run} /> : null}
          {activeStage === "keyframes" ? <KeyframesStage run={run} /> : null}
          {activeStage === "videos" ? <VideosStage run={run} /> : null}
          {activeStage === "assembly" ? <AssemblyStage run={run} /> : null}
        </section>
        <SettingsPanel
          key={[
            run.id,
            run.model,
            run.resolution,
            run.aspect_ratio,
            run.generate_audio,
            run.seed ?? "",
          ].join(":")}
          run={run}
          mobileOpen={settingsOpen}
          onMobileClose={() => setSettingsOpen(false)}
        />
      </main>

      <ProjectMobileTabBar />
    </div>
  );
}

function IdeaStage({ run }: { run: StoryboardRun }) {
  const patch = usePatchStoryboardMutation(run.id, {
    onError: notifyStoryboardError("保存想法"),
  });
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
  const patch = usePatchStoryboardMutation(run.id, {
    onError: notifyStoryboardError("保存脚本"),
  });
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
  const create = useCreateStoryboardAssetMutation(run.id, {
    onError: notifyStoryboardError("新增设定"),
  });
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
        <div className="surface-card grid gap-3 p-3 md:grid-cols-[160px_minmax(0,1fr)]">
          <label className="grid gap-1.5 type-body-sm">
            <span className="type-caption text-[var(--fg-2)]">类型</span>
            <Select
              value={kind}
              onChange={(event) => setKind(event.target.value as "character" | "scene" | "prop")}
            >
              <option value="character">人物</option>
              <option value="scene">场景</option>
              <option value="prop">道具</option>
            </Select>
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
  // 生成设定图会实际调用上游出图（计费动作），失败必须显式告知。
  const generate = useGenerateStoryboardAssetMutation(run.id, asset.id, {
    onError: notifyStoryboardError("生成设定图"),
  });
  const approve = useApproveStoryboardAssetMutation(run.id, asset.id, {
    onError: notifyStoryboardError("批准设定图"),
  });
  const remove = useDeleteStoryboardAssetMutation(run.id, asset.id, {
    onError: notifyStoryboardError("删除设定"),
  });
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
  const rebuild = useRebuildStoryboardShotsMutation(run.id, {
    onError: notifyStoryboardError("从脚本拆分分镜"),
  });
  const create = useCreateStoryboardShotMutation(run.id, {
    onError: notifyStoryboardError("添加镜头"),
  });
  return (
    <StageShell title="分镜" actionLabel="从脚本拆分" loading={rebuild.isPending} onAction={() => rebuild.mutate({ replace: true })}>
      <div className="grid gap-3">
        <button
          type="button"
          onClick={() => create.mutate({ title: `镜头 ${run.shots.length + 1}`, visual: "", duration_s: 5 })}
          className="inline-flex min-h-11 w-fit items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] px-3 text-sm hover:bg-[var(--bg-1)] sm:min-h-10"
        >
          <Plus className="h-4 w-4" />
          手动添加镜头
        </button>
        <div className="grid gap-3">
          {run.shots.map((shot) => (
            <ShotEditor
              key={[
                shot.id,
                shot.title,
                shot.visual,
                shot.narration,
                shot.asset_ids.join(","),
              ].join(":")}
              run={run}
              shot={shot}
            />
          ))}
        </div>
      </div>
    </StageShell>
  );
}

function ShotEditor({ run, shot }: { run: StoryboardRun; shot: StoryboardShot }) {
  const patch = usePatchStoryboardShotMutation(run.id, shot.id, {
    onError: notifyStoryboardError("保存镜头"),
  });
  const approve = useApproveStoryboardShotMutation(run.id, shot.id, {
    onError: notifyStoryboardError("批准镜头"),
  });
  const up = useMoveStoryboardShotMutation(run.id, shot.id, {
    onError: notifyStoryboardError("调整镜头顺序"),
  });
  const remove = useDeleteStoryboardShotMutation(run.id, shot.id, {
    onError: notifyStoryboardError("删除镜头"),
  });
  const [title, setTitle] = useState(shot.title);
  const [visual, setVisual] = useState(shot.visual);
  const [narration, setNarration] = useState(shot.narration);
  const [assetIds, setAssetIds] = useState<string[]>(shot.asset_ids);

  return (
    <article className="grid gap-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/72 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="type-caption text-[var(--fg-3)]">
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
        {run.assets.map((asset) => {
          const selected = assetIds.includes(asset.id);
          return (
          <Button
            key={asset.id}
            variant={selected ? "primary" : "outline"}
            size="sm"
            aria-pressed={selected}
            onClick={() =>
              setAssetIds((current) =>
                selected
                  ? current.filter((id) => id !== asset.id)
                  : [...current, asset.id],
              )
            }
          >
            {asset.name}
          </Button>
          );
        })}
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
  // 批量生成关键帧一次可能派发多张图（计费动作），失败必须显式告知。
  const generateAll = useGenerateAllStoryboardKeyframesMutation(run.id, {
    onError: notifyStoryboardError("批量生成关键帧"),
  });
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
  // 生成关键帧属于计费动作。
  const generate = useGenerateStoryboardKeyframeMutation(run.id, shot.id, {
    onError: notifyStoryboardError("生成关键帧"),
  });
  const approve = useApproveStoryboardKeyframeMutation(run.id, shot.id, {
    onError: notifyStoryboardError("批准关键帧"),
  });
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
