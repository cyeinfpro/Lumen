import assert from "node:assert/strict";
import test from "node:test";
import { draftAfterAcceptedSubmission, failedPreviewStates, hasUnknownOutcome, isInProgress, pendingPreviewStates, privateAudioPath, stateLabel } from "./voice-display.ts";

test("private audio links must remain same-origin API paths", () => {
  assert.equal(privateAudioPath("/speech/jobs/id/audio"), "/api/speech/jobs/id/audio");
  assert.equal(privateAudioPath("/api/voices/id/preview"), "/api/voices/id/preview");
  for (const link of [
    "https://external.test/api/speech/jobs/id/audio",
    "//external.test/speech/jobs/id/audio",
    "/voice/speech/id",
    "/api/speech/../users",
    "/api/speech/%2e%2e/users",
    "/api/speech//external.test",
    "/api/speech/capabilities",
    "/api/speech/jobs/id/audio?token=secret",
  ]) assert.equal(privateAudioPath(link), undefined, link);
});

test("accepting a job does not erase newer composition edits", () => {
  assert.equal(draftAfterAcceptedSubmission("刚提交的稿", "刚提交的稿"), "");
  assert.equal(draftAfterAcceptedSubmission("下一篇稿件", "刚提交的稿"), "下一篇稿件");
});

test("uncertain previews win over pending ones regardless of job order", () => {
  const jobs = [
    { kind: "preview", state: "running", voice_id: "uncertain" },
    { kind: "preview", state: "ambiguous", voice_id: "uncertain" },
    { kind: "preview", state: "queued", voice_id: "pending" },
    { kind: "speech", state: "ambiguous", voice_id: "speech" },
    { kind: "preview", state: "completed", voice_id: "finished" },
  ];
  const expected = new Map([["uncertain", "unknown"], ["pending", "pending"]]);
  assert.deepEqual(pendingPreviewStates(jobs), expected);
  assert.deepEqual(pendingPreviewStates(jobs.toReversed()), expected);
});

test("uncertain work stays distinct from active and terminal work", () => {
  assert.equal(isInProgress("rendering"), true);
  assert.equal(hasUnknownOutcome("ambiguous"), true);
  assert.equal(isInProgress("ambiguous"), false);
  assert.equal(stateLabel("ambiguous"), "结果待确认");
  assert.equal(isInProgress("completed"), false);
});

test("latest preview failure stays visible without replacing a newer result", () => {
  const failure = { kind: "preview", state: "failed", voice_id: "original", output_retrieval_failed: true };
  assert.deepEqual(failedPreviewStates([failure]), new Map([["original", "retrieval"]]));
  assert.equal(failedPreviewStates([{ ...failure, state: "ready" }, failure]).size, 0);
  assert.equal(failedPreviewStates([{ ...failure, state: "running" }, failure]).size, 0);
  assert.deepEqual(failedPreviewStates([{ ...failure, kind: "speech" }]), new Map());
});
