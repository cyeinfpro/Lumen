import assert from "node:assert/strict";
import test from "node:test";
import "./moduleResolution.test-helper.mjs";
import type { AssistantMessage } from "../../lib/types";

const { createVisionSvgSnapshot, visionSvgSendError } = await import(new URL("./visionSvg.ts", import.meta.url).href);
const { createComposerState } = await import(new URL("./composerSlice.ts", import.meta.url).href);
const { visionSvgArtifactFields, applyVisionSvgArtifact } = await import(new URL("../../lib/visionSvg.ts", import.meta.url).href);
const { preferredMessageSnapshot, applyCompletionSnapshot } = await import(new URL("./messageReconciliation.ts", import.meta.url).href);
const { applyCompletionSucceededEvent } = await import(new URL("./completionEvents.ts", import.meta.url).href);
const { gifNativeOutputRows } = await import(new URL("../../lib/gifNativeOutput.ts", import.meta.url).href);

test("explicit vision snapshot preserves native settings and rejects unsupported references", () => {
  const original = { ...createComposerState(null), mode: "image", text: "Rebuild", attachments: [{ id: "ref", mime: "image/png", kind: "uploaded", data_url: "data:image/png;base64,aA==" }] };
  const snapshot = createVisionSvgSnapshot(original);
  assert.equal(snapshot.artifactFormat, "svg");
  assert.equal(snapshot.mode, "chat");
  for (const key of ["fast", "webSearch", "fileSearch", "codeInterpreter", "imageGeneration"]) assert.equal(snapshot[key], false);
  assert.equal(original.mode, "image");
  assert.equal(original.artifactFormat, undefined);
  assert.notEqual(snapshot.params, original.params);
  assert.equal(visionSvgSendError(snapshot, "vision_qa"), null);
  for (const mime of ["image/svg+xml", "image/gif", "application/pdf", ""]) {
    const candidate = { ...snapshot, attachments: [{ ...snapshot.attachments[0], mime }] };
    assert.match(visionSvgSendError(candidate, "vision_qa"), /PNG\/JPEG\/WebP/);
    assert.equal(candidate.attachments[0].mime, mime);
  }
  assert.match(visionSvgSendError({ ...snapshot, attachments: [] }, "vision_qa"), /一张/);
  assert.match(visionSvgSendError({ ...snapshot, attachments: [...snapshot.attachments, ...snapshot.attachments] }, "vision_qa"), /一张/);
  assert.match(visionSvgSendError({ ...snapshot, webSearch: true }, "vision_qa"), /工具/);
  assert.match(visionSvgSendError({ ...snapshot, mask: { image_id: "mask" } }, "vision_qa"), /蒙版/);
});

function assistant(overrides: Partial<AssistantMessage> = {}): AssistantMessage {
  return { id: "a", parent_user_message_id: "u", role: "assistant", intent_resolved: "vision_qa", status: "succeeded", completion_id: "c", created_at: 1, artifact_format: "svg", artifact_status: "processing_failed", ...overrides };
}

test("artifact source is separate from prose and ready state never regresses", () => {
  const source = '<svg onload="alert(1)"><script>unsafe</script></svg>';
  const payload = { artifact_format: "svg", artifact_status: "ready", artifact_image_id: "i", artifact_source_text: source, text: "Validated SVG" } as const;
  assert.deepEqual(visionSvgArtifactFields({ artifact_format: "png" }), {});
  const message = assistant({ text: "Long previous local processing failure message" });
  assert.equal(applyVisionSvgArtifact(message, payload), true);
  assert.equal(message.artifact_source_text, source);
  assert.notEqual(message.text, source);
  assert.equal(applyVisionSvgArtifact(message, { artifact_format: "svg", artifact_status: "pending" }), false);
  const stale = assistant({ artifact_status: "pending" });
  assert.equal(preferredMessageSnapshot(message, stale), message);
  const ready = assistant({ ...payload, artifact_status: "ready", text: "Ready" });
  assert.equal(preferredMessageSnapshot(assistant({ text: "Much longer failure text" }), ready).text, "Ready");
  assert.equal(applyCompletionSnapshot([ready], "c", { id: "c", status: "succeeded", artifact_format: "svg", artifact_status: "processing_failed", text: "stale" }, 20)[0], ready);
});

test("succeeded completion applies local artifact result without treating raw source as answer", () => {
  const message = assistant({ status: "streaming", artifact_status: "pending" });
  const payload = { text: "SVG invalid", artifact_format: "svg", artifact_status: "invalid", artifact_error: "unsafe", artifact_source_text: "<svg><script/></svg>" };
  applyCompletionSucceededEvent(message, payload, 10);
  assert.equal(message.status, "succeeded");
  assert.equal(message.artifact_status, "invalid");
  assert.equal(message.text, "SVG invalid");
  assert.equal(message.artifact_source_text, payload.artifact_source_text);
});

test("GIF actual metadata reports mismatches and encoding rather than inventing budget or playback", () => {
  const rows = gifNativeOutputRows({ output_format: "gif", requested: { size: "512x512", frame_count: 9, frame_duration_ms: 20 }, actual: [{ width: 512, height: 512, frame_count: 4, durations_ms: [40,40,40,40], total_duration_ms: 160, loop: 0 }], mismatches: ["frame_count", "frame_duration_ms"] });
  assert.ok(rows.some((row: {value:string}) => row.value.includes("25 FPS")));
  assert.ok(rows.some((row: {value:string}) => row.value.includes("0.16 秒")));
  assert.ok(rows.some((row: {value:string}) => row.value.includes("不会自动重新生成")));
  assert.ok(rows.some((row: {value:string}) => row.value.includes("不保证播放器帧率")));
  assert.deepEqual(gifNativeOutputRows(null), []);
  assert.deepEqual(gifNativeOutputRows({ output_format: "png", actual: [] }), []);
});

test("GIF zero or missing delays never masquerade as known or uniform frame rate", () => {
  for (const durations_ms of [[0,20], [20], [], undefined, [20,null]]) {
    const rows = gifNativeOutputRows({ output_format: "gif", requested: { frame_duration_ms: 20 }, actual: [{ width: 64, height: 64, frame_count: 2, durations_ms, total_duration_ms: 20 }], mismatches: [] });
    const timing = rows.find((row: {label:string}) => row.label === "文件编码帧率");
    assert.match(timing?.value ?? "", /未知/);
    assert.ok(!JSON.stringify(rows).includes("Infinity"));
    assert.ok(!JSON.stringify(rows).includes("50 FPS"));
  }
  const rows = gifNativeOutputRows({ output_format: "gif", actual: [{ frame_count: 2, durations_ms: [20,40], total_duration_ms: 60 }] });
  assert.ok(rows.some((row: {value:string}) => row.value === "可变：20–40 ms"));
  assert.ok(!rows.some((row: {value:string}) => row.value.includes("FPS")));
});

test("generated reference retains original SVG or GIF MIME despite raster preview", async () => {
  const { createComposerActions } = await import(new URL("./composerSlice.ts", import.meta.url).href);
  for (const mime of ["image/svg+xml", "image/gif", "image/webp"]) {
    let state = { composer: createComposerState(null), imagesById: { result: { id: "result", mime, data_url: "/preview.png", width: 64, height: 64 } } };
    const actions = createComposerActions((partial: unknown) => {
      state = { ...state, ...(typeof partial === "function" ? partial(state) : partial as object) };
    }, () => state, { createInitialComposer: () => createComposerState(null), markFastTouched() {} });
    actions.promoteImageToReference("result");
    assert.equal(state.composer.attachments[0].mime, mime);
    assert.equal(state.composer.attachments[0].source_image_id, "result");
    assert.equal(visionSvgSendError(createVisionSvgSnapshot(state.composer), "vision_qa") === null, mime === "image/webp");
  }
});

test("GIF partial duration sums are unknown and finite loops retain encoded value", () => {
  const incomplete = gifNativeOutputRows({ output_format: "gif", actual: [{ frame_count: 2, durations_ms: [0, 20], total_duration_ms: 20, loop: 3 }] });
  const period = incomplete.find((row: {label:string}) => row.label === "每轮时长");
  assert.match(period?.value ?? "", /未知/);
  assert.ok(!JSON.stringify(incomplete).includes("0.02 秒"));
  assert.ok(incomplete.some((row: {value:string}) => row.value === "有限循环（编码值 3）"));
  const complete = gifNativeOutputRows({ output_format: "gif", actual: [{ frame_count: 2, durations_ms: [20,40], total_duration_ms: 60, loop: 1 }] });
  assert.ok(complete.some((row: {value:string}) => row.value === "0.06 秒"));
  assert.ok(complete.some((row: {value:string}) => row.value === "有限循环（编码值 1）"));
  assert.ok(!JSON.stringify(complete).includes("播放 2 次"));
});
