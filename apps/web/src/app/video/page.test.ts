import { doesNotMatch, match } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = [
  readFileSync(new URL("./page.tsx", import.meta.url), "utf8"),
  readFileSync(new URL("./video-workbench-ui.tsx", import.meta.url), "utf8"),
].join("\n");

test("video workspace keeps history reachable through a responsive task drawer", () => {
  doesNotMatch(source, /xl:overflow-hidden/);
  match(source, /overflow-y-auto overscroll-contain/);
  match(source, /md:grid-cols-\[minmax\(0,1fr\)_300px\]/);
  match(source, /xl:grid-cols-\[minmax\(0,1fr\)_340px\]/);
  match(source, /function VideoTaskDrawer\(/);
  match(source, /useBodyScrollLock\(isTaskPanelOpen/);
  match(source, /mobile-dialog-panel ml-auto flex h-full w-full max-w-\[460px\]/);
  match(source, /onOpenTasks=\{\(\) => setIsTaskPanelOpen\(true\)\}/);
});

test("video task drawer owns its scroll surface instead of shrinking the canvas", () => {
  doesNotMatch(source, /xl:grid-cols-\[minmax\(0,1fr\)_minmax\(320px,380px\)\]/);
  match(source, /<AnimatePresence>/);
  match(source, /mobile-dialog-scroll min-h-0 flex-1 space-y-5 overflow-y-auto/);
  match(source, /activeItems=\{activeItems\}/);
  match(source, /historyItems=\{filteredHistoryItems\}/);
});

test("video prompt and parameter panel use one discoverable workspace scroll", () => {
  match(source, /const resizePromptEditor = useCallback\(\(\) =>/);
  match(source, /target\.style\.height = "0px"/);
  match(source, /target\.style\.height = `\$\{target\.scrollHeight\}px`/);
  match(source, /resize-none overflow-y-hidden/);
  match(source, /className="scroll-mt-20 md:sticky md:top-\[76px\]"/);
  match(source, /id="video-generation-settings"/);
  match(source, /onOpenParameters=\{scrollParametersIntoView\}/);
  match(source, />视频生成参数</);
});

test("video task errors show a readable summary with optional technical details", () => {
  match(source, /function nestedVideoErrorText\(/);
  match(source, /function taskErrorSummary\(/);
  match(source, /参考素材不是有效图片/);
  match(source, /<details className="group mt-2 overflow-hidden/);
  match(source, /技术详情/);
});

test("video prompt enhancement candidates do not trap editor scrolling", () => {
  doesNotMatch(source, /promptEnhancePanelRef/);
  doesNotMatch(
    source,
    /target\.scrollIntoView\(\{ behavior: motionSafeScrollBehavior\(\), block: "start" \}\)/,
  );
  match(source, /function PromptEnhanceChooser\(/);
  match(source, /function PromptEnhanceCandidateCard\(/);
  match(source, /function PromptEnhanceCandidatePreview\(/);
  match(source, /function PromptEnhanceLoadingState\(/);
  match(source, /onReturnToEditor=\{scrollPromptEditorIntoView\}/);
  match(source, /function motionSafeScrollBehavior\(\): ScrollBehavior/);
  match(source, /target\.scrollIntoView\(\{ behavior: motionSafeScrollBehavior\(\), block: "center" \}\)/);
  doesNotMatch(source, /sticky bottom-3/);
  doesNotMatch(source, /max-h-\[min\(72dvh,36rem\)\]/);
  doesNotMatch(source, /max-h-\[min\(42dvh,24rem\)\]/);
  match(source, /选择一个优化方向/);
  match(source, /lg:grid lg:grid-cols-3 lg:overflow-visible/);
  match(source, /完整提示词 · \{candidate\.prompt\.length\.toLocaleString\(\)\} 字/);
  match(source, /focus\(\{ preventScroll: true \}\)/);
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
  match(source, /const REFERENCE_REF_ID_RE = \/\^ref:\(image\|video\|audio\):/);
  match(source, /function referenceDisplayToken\(/);
  match(source, /function serializePromptReferenceMentions\(/);
  match(source, /function displayPromptReferenceMentions\(/);
  match(source, /function normalizePromptReferenceMentions\(/);
  match(source, /function preservePromptReferenceTokens\(/);
  match(source, /anchorPromptEnhanceCandidates\(/);
  match(source, /function promptForVideoAction\(/);
  match(source, /serializePromptReferenceMentions\(trimmed, references\)/);
  match(source, /prompt: promptForVideoAction\(action, prompt, referenceMedia\)/);
  match(source, /referencePromptToken\(item\)/);
  match(source, /insertPromptText\(referenceDisplayToken\(item\)\)/);
  match(source, /return `@/);
  match(source, /function referenceKindNoun\(kind: ReferenceKind\)/);
  match(source, /if \(kind === "audio"\) return "音频"/);
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

test("official asset references keep the selected media kind", () => {
  match(
    source,
    /const \[assetReferenceKind, setAssetReferenceKind\] = useState<ReferenceKind>\("video"\)/,
  );
  match(source, /const assetReferenceKindOptions = useMemo<ReferenceKind\[\]>/);
  match(
    source,
    /isNewApiVideoModel\(selectedModel\) \? REFERENCE_KINDS : \["image", "video"\]/,
  );
  match(source, /aria-pressed=\{active\}/);
  match(source, /onClick=\{\(\) => setAssetReferenceKind\(kind\)\}/);
  match(source, /const selectedAssetReferenceKind = assetReferenceKindOptions\.includes\(/);
  match(source, /const kind = selectedAssetReferenceKind/);
  match(source, /const identity = nextReferenceIdentity\(kind, prev\)/);
  match(source, /kind,/);
  match(source, /toast\.success\(`官方\$\{referenceKindNoun\(kind\)\}已添加`\)/);
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
