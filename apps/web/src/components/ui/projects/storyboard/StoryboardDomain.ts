import type { StoryboardRun } from "@/lib/apiClient";

import { STATUS_TEXT } from "./StoryboardShared";

export type StoryboardStage =
  | "idea"
  | "script"
  | "assets"
  | "shots"
  | "keyframes"
  | "videos"
  | "assembly";

export const STAGES: Array<{
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

export function parseStoryboardStage(
  value: string | null,
): StoryboardStage | null {
  return STAGES.some((stage) => stage.id === value)
    ? (value as StoryboardStage)
    : null;
}

type StageCompletion = {
  done: boolean;
  active: boolean;
  count: string;
};

const SHOT_APPROVED_STATUSES = new Set([
  "approved",
  "keyframe_generating",
  "keyframe_ready",
  "keyframe_approved",
  "generating",
  "done",
]);

function stageResult(
  run: StoryboardRun,
  stage: StoryboardStage,
  done: boolean,
  count: string,
): StageCompletion {
  return { done, active: run.current_stage === stage, count };
}

function countedStageResult(
  run: StoryboardRun,
  stage: StoryboardStage,
  total: number,
  completed: number,
): StageCompletion {
  return stageResult(
    run,
    stage,
    total > 0 && completed === total,
    total ? `${completed}/${total}` : "0",
  );
}

export function stageCompletion(
  run: StoryboardRun,
  stage: StoryboardStage,
): StageCompletion {
  switch (stage) {
    case "idea":
      return stageResult(run, stage, Boolean(run.idea.trim()), "");
    case "script":
      return stageResult(
        run,
        stage,
        run.script_confirmed,
        run.script_confirmed ? "已锁定" : run.script ? "待锁定" : "",
      );
    case "assets":
      return countedStageResult(
        run,
        stage,
        run.assets.length,
        run.assets.filter((asset) => asset.status === "approved").length,
      );
    case "shots":
      return countedStageResult(
        run,
        stage,
        run.shots.length,
        run.shots.filter((shot) => SHOT_APPROVED_STATUSES.has(shot.status)).length,
      );
    case "keyframes":
      return countedStageResult(
        run,
        stage,
        run.shots.length,
        run.shots.filter(
          (shot) => shot.keyframe_approved_at && !shot.keyframe_stale,
        ).length,
      );
    case "videos":
      return countedStageResult(
        run,
        stage,
        run.shots.length,
        run.shots.filter((shot) => shot.status === "done").length,
      );
    case "assembly": {
      const status = run.assembly?.status;
      return stageResult(
        run,
        stage,
        status === "done",
        status ? (STATUS_TEXT[status] ?? status) : "",
      );
    }
  }
}

export function isStageUnlocked(
  run: StoryboardRun,
  stage: StoryboardStage,
): boolean {
  if (stage === "idea") return true;
  if (stage === "script") return Boolean(run.idea.trim());
  if (stage === "assets") return run.script_confirmed;
  if (stage === "shots") return true;
  if (stage === "keyframes") return run.shots.length > 0;
  if (stage === "videos") {
    return (
      run.shots.length > 0 &&
      run.shots.every(
        (shot) => Boolean(shot.keyframe_approved_at) && !shot.keyframe_stale,
      )
    );
  }
  return (
    run.shots.length > 0 &&
    run.shots.every((shot) => shot.status === "done")
  );
}

export function defaultStage(run: StoryboardRun): StoryboardStage {
  if (STAGES.some((stage) => stage.id === run.current_stage)) {
    return run.current_stage as StoryboardStage;
  }
  if (!run.script_confirmed) return "script";
  if (run.assets.length === 0) return "assets";
  if (run.shots.length === 0) return "shots";
  if (
    run.shots.some(
      (shot) => !shot.keyframe_approved_at || shot.keyframe_stale,
    )
  ) {
    return "keyframes";
  }
  if (run.shots.some((shot) => shot.status !== "done")) return "videos";
  return "assembly";
}
