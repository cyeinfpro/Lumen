import {
  deepEqual,
  doesNotMatch,
  equal,
  match,
} from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import type { VideoGenerationOut } from "../../lib/types";

const pageSource = readFileSync(new URL("./page.tsx", import.meta.url), "utf8");
const pageViewSource = readFileSync(
  new URL("./video-page-view.tsx", import.meta.url),
  "utf8",
);
const promptEditorSource = readFileSync(
  new URL("./video-prompt-editor.tsx", import.meta.url),
  "utf8",
);
const directorViewportSource = readFileSync(
  new URL("./video-director-viewport.tsx", import.meta.url),
  "utf8",
);
const controlsSource = readFileSync(
  new URL("./video-workbench-controls.tsx", import.meta.url),
  "utf8",
);
const assetManagerViewSource = readFileSync(
  new URL("./volcano-asset-manager-view.tsx", import.meta.url),
  "utf8",
);
const buttonSource = readFileSync(
  new URL("../../components/ui/primitives/Button.tsx", import.meta.url),
  "utf8",
);
const taskModel = (await import(
  new URL("./video-task-model.ts", import.meta.url).href
)) as typeof import("./video-task-model");

function generationFixture({
  id,
  createdAt,
  status = "succeeded",
  videoId = `${id}-video`,
  videoUrl = "",
}: {
  id: string;
  createdAt: string;
  status?: VideoGenerationOut["status"];
  videoId?: string | null;
  videoUrl?: string;
}): VideoGenerationOut {
  return {
    id,
    status,
    created_at: createdAt,
    video:
      videoId === null
        ? null
        : {
            id: videoId,
            url: videoUrl,
          },
  } as VideoGenerationOut;
}

test("director viewport selects the newest playable successful generation", () => {
  const older = generationFixture({
    id: "older-success",
    createdAt: "2026-08-20T10:00:00Z",
  });
  const newest = generationFixture({
    id: "newest-success",
    createdAt: "2026-08-20T12:00:00Z",
  });
  const unfinished = generationFixture({
    id: "active-with-video",
    createdAt: "2026-08-20T14:00:00Z",
    status: "running",
  });
  const missingVideo = generationFixture({
    id: "success-without-video",
    createdAt: "2026-08-20T15:00:00Z",
    videoId: null,
  });
  const unusableVideo = generationFixture({
    id: "success-without-source",
    createdAt: "2026-08-20T16:00:00Z",
    videoId: "",
    videoUrl: "",
  });

  equal(
    taskModel.selectDirectorViewportVideo([
      unfinished,
      older,
      unusableVideo,
      newest,
      missingVideo,
    ])?.id,
    "newest-success",
  );
  equal(
    taskModel.selectDirectorViewportVideo([unfinished, missingVideo]),
    null,
  );
});

test("director viewport fallback distinguishes ready sources from empty drafts", () => {
  deepEqual(taskModel.directorViewportFallback("t2v", true, "镜头向前推进"), {
    kind: "source",
    title: "镜头描述已就绪",
    description: "完成生成后，最近成片会显示在这里。",
  });
  deepEqual(taskModel.directorViewportFallback("i2v", true, ""), {
    kind: "source",
    title: "首帧素材已就绪",
    description: "完成生成后，最近成片会显示在这里。",
  });
  deepEqual(
    taskModel.directorViewportFallback("reference", false, "", true),
    {
      kind: "loading",
      title: "读取最近成片",
      description: "任务记录读取完成后，将显示可用成片。",
    },
  );
  equal(
    taskModel.directorViewportFallback("reference", false, "").kind,
    "empty",
  );
  equal(
    taskModel.directorViewportFallback("t2v", true, "   ").kind,
    "empty",
  );
});

test("director viewport is a stable real-media stage without fake progress", () => {
  match(pageSource, /selectDirectorViewportVideo\(effectiveItems\)/);
  match(pageSource, /item: directorViewportItem/);
  match(pageSource, /loading: historyQ\.isLoading/);
  match(pageViewSource, /<VideoDirectorViewport/);
  match(directorViewportSource, /relative aspect-video w-full/);
  match(
    directorViewportSource,
    /<video[\s\S]*?controls[\s\S]*?playsInline[\s\S]*?preload="metadata"/,
  );
  match(directorViewportSource, /role="status"/);
  match(directorViewportSource, /role="alert"/);
  doesNotMatch(directorViewportSource, /progressForItem|progress_pct|animate-spin/);
});

test("camera movement categories are native collapsibles with keyboard controls", () => {
  match(pageViewSource, /<VideoPromptEditor model=\{model\.composer\.prompt\} \/>/);
  match(promptEditorSource, /category: "镜头景别"/);
  match(promptEditorSource, /category: "运镜轨迹"/);
  match(promptEditorSource, /category: "光影氛围"/);
  match(
    promptEditorSource,
    /CAMERA_MOVEMENT_LIBRARY\.map\(\(group, index\) => \([\s\S]*?<details/,
  );
  match(promptEditorSource, /<summary className="flex min-h-11/);
  match(promptEditorSource, /role="group"\s+aria-label=\{`\$\{group\.category\}镜头词`\}/);
  match(promptEditorSource, /onClick=\{\(\) => model\.onInsertChip\(chip\)\}/);
  match(promptEditorSource, /disabled=\{model\.enhancing \|\| model\.uploadsPending\}/);
});

test("visual video controls preserve selection and change behavior", () => {
  match(controlsSource, /function VisualAspectRatioPicker\(/);
  match(controlsSource, /function VisualResolutionSelector\(/);
  match(controlsSource, /aria-pressed=\{isSelected\}/);
  match(controlsSource, /onClick=\{\(\) => onChange\(option\)\}/);
  match(controlsSource, /onAspectRatioChange/);
  match(controlsSource, /onResolutionChange/);
  match(controlsSource, /<Select/);
  match(controlsSource, /disabled=\{options\.length === 0\}/);
  match(controlsSource, /options\.length === 0 && <option value="">暂无可用选项/);
  match(controlsSource, /options\.length === 0 && \(/);
  doesNotMatch(controlsSource, /<select/);
  doesNotMatch(assetManagerViewSource, /<select/);
});

test("video submit uses the accessible shared loading primitive", () => {
  const submitStart = controlsSource.indexOf("function SubmitPanel(");
  const submitEnd = controlsSource.indexOf(
    "export function VideoParameterPanelView",
  );
  const submitSource = controlsSource.slice(submitStart, submitEnd);

  match(submitSource, /role="status"/);
  match(submitSource, /aria-live="polite"/);
  match(submitSource, /<Button[\s\S]*?loading=\{loading\}/);
  match(submitSource, /aria-describedby="video-submit-status"/);
  match(submitSource, /disabled=\{!canSubmit\}/);
  doesNotMatch(submitSource, /<button/);
  match(buttonSource, /aria-busy=\{loading \|\| undefined\}/);
});

test("video billing values keep currency and anomaly formatting without truncation", () => {
  const billingStart = controlsSource.indexOf("预计预扣");
  const billingEnd = controlsSource.indexOf("<SubmitPanel", billingStart);
  const billingSource = controlsSource.slice(billingStart, billingEnd);

  match(controlsSource, /amount === "--" \? amount : `¥\$\{amount\}`/);
  match(billingSource, /formatMicroRmb\(estimate\.micro\)/);
  match(billingSource, /formatMicroRmb\(estimate\.unitPriceMicro\)/);
  match(billingSource, /break-all/);
  match(billingSource, /break-words/);
  doesNotMatch(billingSource, /truncate/);
});
