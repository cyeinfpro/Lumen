"use client";

import { Film, Play } from "lucide-react";

import type { StoryboardRun, StoryboardShot } from "@/lib/apiClient";
import {
  useAssembleStoryboardMutation,
  useSubmitAllStoryboardShotsMutation,
  useSubmitStoryboardShotMutation,
} from "@/lib/queries";

import { StoryboardMediaFrame } from "./StoryboardMediaFrame";
import {
  IconAction,
  notifyStoryboardError,
  StageShell,
  StatusPill,
  STATUS_TEXT,
} from "./StoryboardShared";

export function VideosStage({ run }: { run: StoryboardRun }) {
  const submitAll = useSubmitAllStoryboardShotsMutation(run.id, {
    onError: notifyStoryboardError("全部提交视频"),
  });
  return (
    <StageShell
      title="视频"
      actionLabel="全部提交"
      loading={submitAll.isPending}
      onAction={() => submitAll.mutate()}
    >
      <div className="grid gap-2">
        {run.shots.map((shot) => (
          <VideoQueueRow key={shot.id} run={run} shot={shot} />
        ))}
      </div>
    </StageShell>
  );
}

function VideoQueueRow({
  run,
  shot,
}: {
  run: StoryboardRun;
  shot: StoryboardShot;
}) {
  const submit = useSubmitStoryboardShotMutation(run.id, shot.id, {
    onError: notifyStoryboardError("提交视频段"),
  });
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
          <span className="type-caption text-[var(--fg-3)]">
            段 {String(shot.index).padStart(2, "0")}
          </span>
          <StatusPill status={shot.video_status || shot.status} />
        </div>
        <h3 className="mt-1 truncate type-body-sm font-semibold">{shot.title}</h3>
        <div className="mt-2 h-2 overflow-hidden rounded-full bg-[var(--bg-2)]">
          <div
            className="h-full bg-[var(--accent)] transition-all"
            style={{ width: `${Math.max(0, Math.min(100, pct))}%` }}
          />
        </div>
      </div>
      <div className="flex gap-2">
        {shot.video?.url ? (
          <a
            href={shot.video.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex min-h-11 items-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] px-3 type-caption hover:bg-[var(--bg-2)] sm:min-h-9"
          >
            <Play className="h-3.5 w-3.5" />
            预览
          </a>
        ) : null}
        <IconAction
          icon={Film}
          label="提交"
          disabled={!canSubmitVideo}
          loading={submit.isPending}
          onClick={() => submit.mutate()}
        />
      </div>
    </article>
  );
}

export function AssemblyStage({ run }: { run: StoryboardRun }) {
  const assemble = useAssembleStoryboardMutation(run.id, {
    onError: notifyStoryboardError("合成成片"),
  });
  const ready =
    run.shots.length > 0 &&
    run.shots.every((shot) => shot.status === "done");
  return (
    <StageShell
      title="成片"
      actionLabel="合成成片"
      loading={assemble.isPending}
      disabled={!ready}
      onAction={() => assemble.mutate()}
    >
      <div className="grid gap-4 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/74 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="type-body-sm font-semibold">合成状态</p>
            <p className="mt-1 type-body-sm text-[var(--fg-1)]">
              {STATUS_TEXT[run.assembly?.status || "waiting_input"] ??
                run.assembly?.status ??
                "等待视频段完成"}
            </p>
          </div>
          <StatusPill status={run.assembly?.status || "waiting_input"} />
        </div>
        {run.assembly?.video_url ? (
          <video
            src={run.assembly.video_url}
            poster={run.assembly.poster_url || undefined}
            controls
            className="max-h-[62vh] w-full rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]"
          />
        ) : (
          <div className="grid min-h-52 place-items-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] text-center text-[var(--fg-2)]">
            {ready
              ? "所有片段已完成，可以合成成片。"
              : "所有视频段完成后才能合成成片。"}
          </div>
        )}
        {run.assembly?.video_url ? (
          <a
            href={run.assembly.video_url}
            download
            className="inline-flex min-h-11 w-fit items-center justify-center rounded-[var(--radius-control)] bg-[var(--accent)] px-4 type-body-sm font-semibold text-[var(--accent-on)] sm:min-h-10"
          >
            下载 mp4
          </a>
        ) : null}
      </div>
    </StageShell>
  );
}
