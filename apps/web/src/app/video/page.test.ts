import { doesNotMatch, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(new URL("./page.tsx", import.meta.url), "utf8");

test("video history remains reachable below xl layouts", () => {
  match(source, /md:overflow-y-auto/);
  match(source, /xl:overflow-hidden/);
  match(source, /xl:grid-cols-\[minmax\(0,1fr\)_minmax\(320px,380px\)\]/);
  match(source, /xl:sticky xl:top-4/);
});

test("video task list only becomes an internal scroller in xl side-panel layouts", () => {
  doesNotMatch(source, /max-h-\[720px\][^"]*overflow-hidden/);
  match(source, /xl:h-\[min\(720px,calc\(100dvh-5rem\)\)\] xl:overflow-hidden/);
  match(source, /xl:min-h-0 xl:flex-1 xl:overflow-y-auto xl:overscroll-contain/);
});

test("video prompt enhancement candidates do not trap editor scrolling", () => {
  doesNotMatch(source, /promptEnhancePanelRef/);
  doesNotMatch(source, /block: "start"/);
  match(source, /function PromptEnhanceChooser\(/);
  match(source, /onReturnToEditor=\{scrollPromptEditorIntoView\}/);
  match(source, /target\.scrollIntoView\(\{ behavior: "smooth", block: "center" \}\)/);
  match(source, /max-h-\[clamp\(14rem,60dvh,34rem\)\][^"]*overflow-y-auto/);
  match(source, /回到编辑/);
  match(source, /pb-\[calc\(var\(--mobile-tabbar-height\)\+1rem\)\]/);
  match(source, /pb-\[calc\(var\(--mobile-tabbar-height\)\+2rem\)\]/);
  match(source, /scroll-mt-4 md:scroll-mt-6/);
});

test("video prompt enhancement copy stays video-model specific", () => {
  match(source, /动作轨迹、镜头运动、首尾时间推进、参考素材怎么用/);
  match(source, /按火山视频结构补动作、运镜和参考一致性/);
});

test("video prompt enhancement respects Vibe Creating non-rewrite actions", () => {
  match(source, /type PromptEnhanceAction =/);
  match(source, /action === "ask_first"/);
  match(source, /action === "keep_original"/);
  match(source, /action === "optional_vc"/);
  match(source, /function shouldAutoApplyPromptEnhanceCandidate/);
  match(source, /function canApplyPromptEnhanceCandidate/);
  match(source, /未自动替换/);
  match(source, /仅查看/);
});

test("video duration selector follows selected model action and resolution", () => {
  match(source, /durations_by_action_resolution\?\.\[action\]\?\.\[resolution\]/);
  match(source, /durations_by_action\?\.\[action\]/);
  match(source, /function durationOrPreferred\(current: number, options: number\[\]\)/);
  match(source, /setDurationS\(\(prev\) =>\s*durationOrPreferred\(prev, nextDurations\),\s*\)/);
});

test("video temporary upstream download is available before local storage", () => {
  match(source, /function activeTemporaryDownload\(item: VideoGenerationOut\)/);
  match(source, /const canDownload = videoItem != null \|\| activeTemporaryDownload\(item\) != null/);
  match(source, /target=\{isTemporary \? "_blank" : undefined\}/);
  match(source, /\{isTemporary \? "快速下载" : "下载"\}/);
});

test("video task rows and preview show elapsed runtime", () => {
  match(source, /function formatTaskElapsed\(ms\?: number \| null\): string \| null/);
  match(source, /function taskElapsedLabel\(item: VideoGenerationOut\): string \| null/);
  match(source, /\$\{isTerminalVideo\(item\) \? "耗时" : "已耗时"\} \$\{elapsed\}/);
  match(source, /const elapsedLabel = taskElapsedLabel\(item\)/);
  match(source, /\{elapsedLabel && <span>\{elapsedLabel\}<\/span>\}/);
});
