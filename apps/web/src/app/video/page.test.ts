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
  match(source, /sticky bottom-3/);
  match(source, /max-h-\[min\(72dvh,36rem\)\]/);
  match(source, /xl:grid-cols-\[minmax\(220px,280px\)_minmax\(0,1fr\)\]/);
  match(source, /overflow-y-auto whitespace-pre-wrap/);
  match(source, /回到编辑/);
  match(source, /pb-\[calc\(var\(--mobile-tabbar-height\)\+1rem\)\]/);
  match(source, /pb-\[calc\(var\(--mobile-tabbar-height\)\+2rem\)\]/);
  match(source, /scroll-mt-4 md:scroll-mt-6/);
});

test("video prompt enhancement copy stays video-model specific", () => {
  match(source, /动作轨迹、镜头运动、首尾时间推进/);
  match(source, /点击参考素材插入 @图片1 \/ @视频1/);
  match(source, /按火山视频结构补动作、运镜和参考一致性/);
});

test("video reference prompts use stable anchor ids through enhancement", () => {
  match(source, /const REFERENCE_REF_ID_RE = \/\^ref:\(image\|video\):/);
  match(source, /function referenceDisplayToken\(/);
  match(source, /function serializePromptReferenceMentions\(/);
  match(source, /function displayPromptReferenceMentions\(/);
  match(source, /function normalizePromptReferenceMentions\(/);
  match(source, /function preservePromptReferenceTokens\(/);
  match(source, /anchorPromptEnhanceCandidates\(/);
  match(source, /serializePromptReferenceMentions\(prompt\.trim\(\), referenceMedia\)/);
  match(source, /referencePromptToken\(item\)/);
  match(source, /insertPromptText\(referenceDisplayToken\(item\)\)/);
  match(source, /return `@/);
  match(source, /item\.kind === "image" \? "图片" : "视频"/);
  match(source, /displayPromptReferenceMentions\(value, referenceMedia\)/);
  match(source, /referenceRefId\(item\.kind, fallbackIndex\)/);
  match(source, /视频素材 \$\{index\}/);
  match(source, /动作参考 \$\{index\}/);
  match(source, /这段素材/);
});

test("video reference chips render material thumbnails", () => {
  match(source, /previewUrl\?: string \| null/);
  match(source, /function imageReferencePreviewUrl\(/);
  match(source, /imageVariantUrl\(image\.id, "display2048"\)/);
  match(source, /imageVariantUrl\(ref\.image_id, "display2048"\)/);
  match(source, /video\.poster_url\) \?\? videoPosterUrl\(video\.id\)/);
  match(source, /function ReferenceThumbnail\(/);
  match(source, /<ReferenceThumbnail item=\{item\} active=\{active\} \/>/);
  match(source, /w-\[min\(82vw,19rem\)\]/);
  match(source, /h-24 w-32/);
  match(source, /<img\s+src=\{previewUrl \?\? ""\}/);
  match(source, /function ReferenceMediaPreviewDialog\(/);
  match(source, /onPreview=\{\(\) => setReferencePreviewItem\(item\)\}/);
  match(source, /查看 \$\{displayToken\} 预览/);
  match(source, /promptContainsReferenceMention\(prompt, item\)/);
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
