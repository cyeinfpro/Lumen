import { deepEqual, equal, match } from "node:assert/strict";
import { test } from "node:test";
import type { VideoGenerationOut } from "../../lib/types";

const { stageCopy, videoHistoryLoadError } = await import(new URL("./video-task-model.ts", import.meta.url).href);
const { mergeVideoGenerationEvent, mergeVideoGenerationSnapshot } = await import(new URL("../../lib/videoEventSnapshot.ts", import.meta.url).href);
const at = "2026-09-05T12:00:00Z";
const later = "2026-09-05T12:01:00Z";
function task(overrides: Partial<VideoGenerationOut> = {}): VideoGenerationOut {
  return {
    id: "media-task", action: "t2v", model: "seedance", prompt: "A scene", reference_media: [],
    duration_s: 5, resolution: "720p", aspect_ratio: "16:9", generate_audio: true,
    status: "running", progress_stage: "rendering", progress_pct: 4, submission_epoch: 2,
    est_token_upper: 1, est_cost: { micro: 1, rmb: "0.000001" }, created_at: at, updated_at: at,
    ...overrides,
  };
}

test("media terminal status takes precedence over stale finished/rendering stage and cancel intent", () => {
  for (const [status, label] of [["failed", "失败"], ["canceled", "已取消"], ["expired", "已过期"]] as const) {
    for (const progress_stage of ["finished", "rendering"] as const) {
      equal(stageCopy(task({ status, progress_stage, cancel_requested_at: at })).label, label);
    }
  }
  equal(stageCopy(task({ status: "succeeded", video: null, cancel_requested_at: at })).label, "整理中");
  equal(stageCopy(task({ status: "succeeded", video: { id: "saved" } as VideoGenerationOut["video"] })).label, "可下载");
});

test("media pending and unknown states report stages without inventing completion", () => {
  equal(stageCopy(task({ status: "queued", progress_stage: "queued", progress_pct: 0 })).label, "排队中");
  equal(stageCopy(task({ status: "submit_unknown", progress_stage: "submitting" })).label, "状态待确认");
  equal(stageCopy(task({ progress_stage: "storing" })).label, "保存中");
  equal(stageCopy(task({ progress_stage: "billing" })).label, "结算中");
  deepEqual(stageCopy(task({ progress_pct: 0 })), stageCopy(task({ progress_pct: 98 })));
});

test("media cancellation receipt persists through refetch and same-epoch events but not a retry epoch", () => {
  const original = task();
  const accepted = mergeVideoGenerationSnapshot(original, task({ cancel_requested_at: at, updated_at: later }));
  equal(stageCopy(accepted).label, "已请求取消");
  match(stageCopy(accepted).detail, /仍可能完成并计费/);
  const stale = mergeVideoGenerationSnapshot(accepted, task({ progress_stage: "fetching", cancel_requested_at: null }));
  equal(stale.cancel_requested_at, at);
  const sameEpoch = mergeVideoGenerationEvent(stale, { video_generation_id: original.id, submission_epoch: 2, stage: "storing" });
  equal(sameEpoch.cancel_requested_at, at);
  const retried = mergeVideoGenerationEvent(sameEpoch, { video_generation_id: original.id, submission_epoch: 3, status: "submitting", stage: "submitting" });
  equal(retried.cancel_requested_at, null);
  equal(stageCopy(retried).label, "提交中");
  const lateCancel = mergeVideoGenerationSnapshot(retried, accepted);
  equal(lateCancel, retried);
  const nextSnapshot = mergeVideoGenerationSnapshot(accepted, task({ submission_epoch: 3, status: "queued", progress_stage: "queued" }));
  equal(nextSnapshot.cancel_requested_at, null);
});

test("media video history distinguishes empty success, offline, permission and server failure", () => {
  equal(videoHistoryLoadError(null, false), null);
  match(videoHistoryLoadError(null, true), /离线/);
  match(videoHistoryLoadError({ status: 403 }, false), /权限/);
  match(videoHistoryLoadError({ status: 401 }, false), /登录/);
  match(videoHistoryLoadError({ status: 0 }, false), /网络/);
  match(videoHistoryLoadError({ status: 500 }, false), /服务/);
});
