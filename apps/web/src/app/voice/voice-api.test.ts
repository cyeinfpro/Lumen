import assert from "node:assert/strict";
import test from "node:test";
import { draftAfterAcceptedSubmission, hasUnknownOutcome, isInProgress, privateAudioPath, stateLabel } from "./voice-display.ts";

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

test("uncertain work stays distinct from active and terminal work", () => {
  assert.equal(isInProgress("rendering"), true);
  assert.equal(hasUnknownOutcome("ambiguous"), true);
  assert.equal(isInProgress("ambiguous"), false);
  assert.equal(stateLabel("ambiguous"), "结果待确认");
  assert.equal(isInProgress("completed"), false);
});
