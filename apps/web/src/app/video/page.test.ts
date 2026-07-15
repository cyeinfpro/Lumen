import { deepEqual, doesNotMatch, equal, match, ok } from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import type {
  VideoGenerationOut,
  VideoReferenceMediaIn,
} from "../../lib/types";

const pageSource = readFileSync(new URL("./page.tsx", import.meta.url), "utf8");
const referenceDomainSource = readFileSync(
  new URL("./video-reference-domain.ts", import.meta.url),
  "utf8",
);
const volcanoAssetDomainSource = readFileSync(
  new URL("./volcano-asset-domain.ts", import.meta.url),
  "utf8",
);
const nextConfigSource = readFileSync(
  new URL("../../../next.config.ts", import.meta.url),
  "utf8",
);
const taskModelSource = readFileSync(
  new URL("./video-task-model.ts", import.meta.url),
  "utf8",
);
const taskUiSource = readFileSync(
  new URL("./video-task-ui.tsx", import.meta.url),
  "utf8",
);
const optionsModelSource = readFileSync(
  new URL("./video-options-model.ts", import.meta.url),
  "utf8",
);
const source = [
  pageSource,
  referenceDomainSource,
  optionsModelSource,
  readFileSync(new URL("./video-page-utils.ts", import.meta.url), "utf8"),
  readFileSync(new URL("./video-workbench-ui.tsx", import.meta.url), "utf8"),
  readFileSync(
    new URL("./video-request-lifecycle.ts", import.meta.url),
    "utf8",
  ),
  taskModelSource,
  taskUiSource,
  readFileSync(
    new URL("../../lib/videoEventSnapshot.ts", import.meta.url),
    "utf8",
  ),
].join("\n");
const referenceDomainUrl = new URL(
  "./video-reference-domain.ts",
  import.meta.url,
);
const referenceDomain = (await import(
  referenceDomainUrl.href
)) as typeof import("./video-reference-domain");
const taskModelUrl = new URL("./video-task-model.ts", import.meta.url);
const taskModel = (await import(
  taskModelUrl.href
)) as typeof import("./video-task-model");

type TestReference = VideoReferenceMediaIn & {
  label: string;
  ref_id: string;
};

const testReferences: TestReference[] = [
  {
    kind: "image",
    image_id: "image-1",
    label: "主体图",
    ref_id: "ref:image:1",
  },
  {
    kind: "video",
    video_id: "video-1",
    label: "动作视频",
    ref_id: "ref:video:1",
  },
  {
    kind: "audio",
    url: "asset://asset-audio-1",
    label: "配乐",
    ref_id: "ref:audio:1",
  },
];

function taskFixture(
  overrides: Partial<VideoGenerationOut> = {},
): VideoGenerationOut {
  return {
    status: "queued",
    progress_stage: "queued",
    progress_pct: 0,
    elapsed_ms: null,
    video: null,
    ...overrides,
  } as VideoGenerationOut;
}

test("video task domain stays extracted from the route page", () => {
  ok(pageSource.split("\n").length < 3000);
  doesNotMatch(pageSource, /function VideoTaskDrawer\(/);
  doesNotMatch(pageSource, /function VideoPreviewDialog\(/);
  match(pageSource, /from "\.\/video-task-model"/);
  match(pageSource, /from "\.\/video-task-ui"/);
  match(taskUiSource, /export function VideoTaskDrawer\(/);
  match(taskUiSource, /export function VideoPreviewDialog\(/);
  match(taskModelSource, /export function isActiveVideo\(/);
});

test("video option and pricing domain stays extracted from the route page", () => {
  ok(pageSource.split("\n").length <= 2425);
  doesNotMatch(pageSource, /function durationOptionsForModel\(/);
  doesNotMatch(pageSource, /function estimateHoldMicro\(/);
  match(pageSource, /from "\.\/video-options-model"/);
  match(optionsModelSource, /export function durationOptionsForModel\(/);
  match(optionsModelSource, /export function estimateHoldMicro\(/);
});

test("video mobile surfaces preserve safe-area, scroll, and touch contracts", () => {
  match(
    pageSource,
    /scroll-padding-bottom:calc\(var\(--mobile-tabbar-height\)\+6rem\)/,
  );
  match(pageSource, /pb-\[calc\(var\(--mobile-tabbar-height\)\+1rem\)\]/);
  match(taskUiSource, /mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto/);
  match(taskUiSource, /min-h-11 rounded-\[var\(--radius-control\)\]/);
  match(taskUiSource, /\[&_button\]:min-h-11/);
  match(source, /mobile-dialog-footer/);
  match(source, /landscape:max-sm/);
});

test("video reference domain stays extracted from the route page", () => {
  doesNotMatch(pageSource, /function referenceRefId\(/);
  doesNotMatch(pageSource, /function referenceRefIndex\(/);
  doesNotMatch(pageSource, /function normalizePromptReferenceMentions\(/);
  doesNotMatch(pageSource, /function promptForVideoAction\(/);
  match(pageSource, /from "\.\/video-reference-domain"/);
  match(referenceDomainSource, /export function referenceRefId\(/);
  match(
    referenceDomainSource,
    /export function serializePromptReferenceMentions\(/,
  );
  match(referenceDomainSource, /export function promptForVideoAction\(/);
});

test("video reference identity, labels, limits, and asset ids are stable", () => {
  equal(referenceDomain.referenceRefId("video", 3), "ref:video:3");
  equal(referenceDomain.referenceRefIndex(" REF:VIDEO:3 ", "video"), 3);
  equal(referenceDomain.referenceRefIndex("ref:image:3", "video"), null);
  equal(referenceDomain.referenceRefIndex("ref:image:1000", "image"), null);
  equal(referenceDomain.referenceKindNoun("audio"), "音频");
  equal(referenceDomain.referenceLabel("image", 4), "图片 4");
  deepEqual(
    referenceDomain.nextReferenceIdentity("image", [
      testReferences[0],
      { kind: "image", ref_id: "ref:image:3" },
      { kind: "video", ref_id: "ref:video:8" },
    ]),
    { refId: "ref:image:4", label: "图片 4" },
  );
  deepEqual(referenceDomain.referenceCountsFor(testReferences), {
    image: 1,
    video: 1,
    audio: 1,
  });
  equal(
    referenceDomain.referenceLimitViolation(
      [...testReferences, { kind: "audio" }],
      { image: 9, video: 3, audio: 1 },
    ),
    "参考音频最多 1 个",
  );
  deepEqual(referenceDomain.referenceLimitsForModel("video_ds_2.0-pro"), {
    image: 4,
    video: 3,
    audio: 1,
  });
  deepEqual(referenceDomain.referenceLimitsForModel("wan-video"), {
    image: 9,
    video: 3,
    audio: 1,
  });
  deepEqual(
    referenceDomain.referenceLimitsForModelOption(
      {
        model: "seedance-2.0",
        actions: ["reference"],
        reference_media_limits: { image: 6, video: 2 },
      },
      "seedance-2.0",
    ),
    {
      image: 6,
      video: 2,
      audio: 0,
    },
  );
  deepEqual(
    referenceDomain.referenceLimitsForModelOption(
      {
        model: "seedance-2.0",
        actions: ["reference"],
      },
      "seedance-2.0",
    ),
    {
      image: 9,
      video: 3,
      audio: 1,
    },
  );
  equal(
    referenceDomain.normalizeAssetUrl(' "ASSET://Asset-ABC_1" '),
    "asset://asset-abc_1",
  );
  equal(
    referenceDomain.assetIdFromReferenceUrl("asset://Asset-ABC_1"),
    "asset-abc_1",
  );
  equal(referenceDomain.assetIdFromReferenceUrl("https://example.com"), null);
  equal(referenceDomain.normalizeAssetUrl("https://example.com/asset-1"), "");
});

test("video reference aliases serialize to anchors and display round trips", () => {
  const imageReference = testReferences[0];
  const videoReference = testReferences[1];
  equal(
    referenceDomain.referencePromptToken({
      kind: "image",
      ref_id: " REF:IMAGE:2 ",
    }),
    "[ref:image:2]",
  );
  equal(
    referenceDomain.referencePromptToken(
      { kind: "audio", ref_id: "invalid" },
      3,
    ),
    "[ref:audio:3]",
  );
  equal(referenceDomain.referenceDisplayToken(videoReference), "@视频1");
  ok(referenceDomain.referenceDisplayAliases(imageReference).includes("@图 1"));
  ok(
    referenceDomain
      .referenceMentionAliases(videoReference)
      .includes("第一段素材"),
  );

  const serialized = referenceDomain.serializePromptReferenceMentions(
    "让 @图1 保持主体，并模仿这段素材。",
    [imageReference, videoReference],
  );
  equal(
    serialized,
    "让 [ref:image:1] 保持主体，并模仿这段素材 [ref:video:1]。",
  );
  equal(
    referenceDomain.displayPromptReferenceMentions(serialized, [
      imageReference,
      videoReference,
    ]),
    "让 @图片1 保持主体，并模仿这段素材 @视频1。",
  );
  equal(
    referenceDomain.normalizePromptReferenceMentions("第一张图片作为主体", [
      imageReference,
    ]),
    "第一张图片 [ref:image:1]作为主体",
  );
  equal(
    referenceDomain.normalizePromptReferenceMentions("这张图作为主体", [
      imageReference,
      {
        kind: "image",
        label: "背景图",
        ref_id: "ref:image:2",
      },
    ]),
    "这张图作为主体",
  );
  equal(
    referenceDomain.promptContainsReferenceMention(
      "使用 [ref:image:1]",
      imageReference,
    ),
    true,
  );
  equal(
    referenceDomain.promptContainsReferenceMention(
      "使用 @图 1",
      imageReference,
    ),
    true,
  );

  const imageTwo = {
    kind: "image" as const,
    image_id: "image-2",
    label: "图片 2",
    ref_id: "ref:image:2",
  };
  deepEqual(
    referenceDomain.removeReferenceAndReindexPrompt(
      "让 @图片2 跟随 @视频1，同时保留 @图片1。",
      [imageReference, imageTwo, videoReference],
      imageReference,
    ),
    {
      prompt: "让 @图片1 跟随 @视频1，同时保留。",
      references: [
        {
          ...imageTwo,
          label: "图片 1",
          ref_id: "ref:image:1",
        },
        videoReference,
      ],
    },
  );
  deepEqual(
    referenceDomain.removeReferenceAndReindexPrompt(
      "主体图保持自然语言描述；[主体图] @图片1",
      [imageReference],
      imageReference,
    ),
    {
      prompt: "主体图保持自然语言描述；",
      references: [],
    },
  );
  deepEqual(
    referenceDomain.removeReferencesAndReindexPrompt(
      "@图片1 @图片2 @视频1",
      [imageReference, imageTwo, videoReference],
      (item) => item !== imageTwo,
    ),
    {
      prompt: "@图片1",
      references: [
        {
          ...imageTwo,
          label: "图片 1",
          ref_id: "ref:image:1",
        },
      ],
    },
  );
});

test("video prompt enhancement and request serialization preserve anchors", () => {
  const references = testReferences.slice(0, 2);
  const candidates = [
    {
      id: "variant-1",
      title: "动作版",
      prompt: "主体转身",
      action: "direct_rewrite" as const,
    },
  ];
  const anchored = referenceDomain.anchorPromptEnhanceCandidates(
    candidates,
    "使用 [ref:image:1] 与 [ref:video:1]",
    references,
  );
  deepEqual(anchored, [
    {
      ...candidates[0],
      prompt:
        "主体转身。保持参考锚点 [ref:image:1]、[ref:video:1] 对应的素材约束。",
    },
  ]);
  deepEqual(
    referenceDomain.displayPromptEnhanceCandidates(anchored, references),
    [
      {
        ...candidates[0],
        prompt: "主体转身。保持参考锚点 @图片1、@视频1 对应的素材约束。",
      },
    ],
  );
  equal(
    referenceDomain.promptForVideoAction(
      "reference",
      "  @图片1 跟随 @视频1  ",
      references,
    ),
    "[ref:image:1] 跟随 [ref:video:1]",
  );
  equal(
    referenceDomain.promptForVideoAction(
      "t2v",
      "  @图片1 跟随 @视频1  ",
      references,
    ),
    "@图片1 跟随 @视频1",
  );
  deepEqual(
    referenceDomain.referencePayloadForVideoAction("reference", testReferences),
    [
      {
        kind: "image",
        image_id: "image-1",
        video_id: null,
        label: "主体图",
        ref_id: "ref:image:1",
      },
      {
        kind: "video",
        image_id: null,
        video_id: "video-1",
        label: "动作视频",
        ref_id: "ref:video:1",
      },
      {
        kind: "audio",
        url: "asset://asset-audio-1",
        label: "配乐",
        ref_id: "ref:audio:1",
      },
    ],
  );
  deepEqual(
    referenceDomain.referencePayloadForVideoAction("i2v", testReferences),
    [],
  );
});

test("video task model preserves status, elapsed, and error semantics", () => {
  equal(taskModel.isActiveVideo(taskFixture()), true);
  equal(
    taskModel.isActiveVideo(
      taskFixture({ status: "succeeded", progress_stage: "fetching" }),
    ),
    true,
  );
  equal(taskModel.isTerminalVideo(taskFixture({ status: "succeeded" })), true);
  equal(
    taskModel.isFailedHistoryVideo(taskFixture({ status: "canceled" })),
    true,
  );
  equal(taskModel.formatTaskElapsed(65_000), "1m 5s");
  equal(taskModel.formatTaskElapsed(-1), null);
  equal(
    taskModel.taskElapsedLabel(taskFixture({ elapsed_ms: 65_000 })),
    "已耗时 1m 5s",
  );
  equal(
    taskModel.taskElapsedLabel(
      taskFixture({ status: "succeeded", elapsed_ms: 65_000 }),
    ),
    "耗时 1m 5s",
  );
  equal(
    taskModel.taskErrorSummary(
      '{"detail":"specified asset is not an image. Request id: req-1"}',
    ),
    "参考素材不是有效图片，请检查素材类型或重新上传后再试。",
  );
  deepEqual(taskModel.videoHistoryEmptyCopy("failed", 1, false), {
    title: "暂无失败记录",
    description: "当前任务完成后会进入历史。",
  });
});

test("video workspace keeps history reachable through a responsive task drawer", () => {
  doesNotMatch(source, /xl:overflow-hidden/);
  match(source, /overflow-y-auto overscroll-contain/);
  match(source, /md:grid-cols-\[minmax\(0,1fr\)_300px\]/);
  match(source, /xl:grid-cols-\[minmax\(0,1fr\)_340px\]/);
  match(source, /function VideoTaskDrawer\(/);
  match(source, /useBodyScrollLock\(isTaskPanelOpen/);
  match(
    source,
    /mobile-dialog-panel ml-auto flex h-full w-full max-w-\[460px\]/,
  );
  match(source, /onOpenTasks=\{\(\) => setIsTaskPanelOpen\(true\)\}/);
});

test("video task drawer owns its scroll surface instead of shrinking the canvas", () => {
  doesNotMatch(
    source,
    /xl:grid-cols-\[minmax\(0,1fr\)_minmax\(320px,380px\)\]/,
  );
  match(source, /<AnimatePresence>/);
  match(
    source,
    /mobile-dialog-scroll min-h-0 flex-1 space-y-5 overflow-y-auto/,
  );
  match(source, /activeItems=\{activeItems\}/);
  match(source, /historyItems=\{filteredHistoryItems\}/);
});

test("video prompt and parameter panel use one discoverable workspace scroll", () => {
  match(source, /const resizePromptEditor = useCallback\(\(\) =>/);
  match(source, /target\.style\.height = "0px"/);
  match(source, /target\.style\.height = `\$\{target\.scrollHeight\}px`/);
  match(source, /resize-none overflow-y-hidden/);
  match(
    source,
    /className="scroll-mt-20 pb-\[calc\(var\(--mobile-tabbar-height\)\+1rem\)\] md:sticky md:top-\[76px\] md:pb-0"/,
  );
  match(source, /id="video-generation-settings"/);
  match(source, /onOpenParameters=\{scrollParametersIntoView\}/);
  match(source, />\s*视频生成参数\s*</);
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
  match(
    source,
    /target\.scrollIntoView\(\{\s*behavior: motionSafeScrollBehavior\(\),\s*block: "center",?\s*\}\)/,
  );
  doesNotMatch(source, /sticky bottom-3/);
  doesNotMatch(source, /max-h-\[min\(72dvh,36rem\)\]/);
  doesNotMatch(source, /max-h-\[min\(42dvh,24rem\)\]/);
  match(source, /选择一个优化方向/);
  match(source, /lg:grid lg:grid-cols-3 lg:overflow-visible/);
  match(
    source,
    /完整提示词 · \{candidate\.prompt\.length\.toLocaleString\(\)\} 字/,
  );
  match(source, /focus\(\{ preventScroll: true \}\)/);
  match(source, /回到编辑/);
  match(source, /pb-\[calc\(var\(--mobile-tabbar-height\)\+1rem\)\]/);
  match(
    source,
    /scroll-padding-bottom:calc\(var\(--mobile-tabbar-height\)\+6rem\)/,
  );
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
  match(
    source,
    /prompt: promptForVideoAction\(action, prompt, referenceMedia\)/,
  );
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
  match(
    source,
    /cleanReferencePreviewUrl\(video\.poster_url\)\s*\?\?\s*videoPosterUrl\(video\.id\)/,
  );
  match(source, /function ReferenceThumbnail\(/);
  match(source, /<ReferenceThumbnail item=\{item\} active=\{active\} \/>/);
  match(source, /w-\[min\(82vw,19rem\)\]/);
  match(source, /h-24 w-32/);
  match(source, /<img\s+src=\{previewUrl \?\? ""\}/);
  match(source, /function ReferenceMediaPreviewDialog\(/);
  match(source, /onPreview=\{\(\) => setReferencePreviewItem\(item\)\}/);
  match(source, /查看 \$\{displayToken\} 预览/);
  match(source, /promptContainsReferenceMention\(\s*prompt,\s*item,\s*\)/);
});

test("official asset references keep the selected media kind", () => {
  match(
    source,
    /const \[assetReferenceKind, setAssetReferenceKind\] =\s*useState<ReferenceKind>\("video"\)/,
  );
  match(source, /const assetReferenceKindOptions = useMemo<ReferenceKind\[\]>/);
  match(
    source,
    /REFERENCE_KINDS\.filter\(\(kind\) => referenceLimits\[kind\] > 0\)/,
  );
  match(source, /aria-pressed=\{active\}/);
  match(source, /onClick=\{\(\) => setAssetReferenceKind\(kind\)\}/);
  match(
    source,
    /const selectedAssetReferenceKind = assetReferenceKindOptions\.includes\(/,
  );
  match(source, /assetReferenceKindOptions\[0\] \?\? "image"/);
  match(source, /const kind = selectedAssetReferenceKind/);
  match(source, /const identity = nextReferenceIdentity\(kind, references\)/);
  match(source, /kind,/);
  match(
    source,
    /toast\.success\(`官方\$\{referenceKindNoun\(kind\)\}已添加`\)/,
  );
});

test("volcano virtual asset manager is integrated with reference drafts", () => {
  match(pageSource, /from "\.\/volcano-asset-manager"/);
  match(pageSource, />\s*火山虚拟素材库\s*</);
  match(pageSource, /existingVolcanoAssetIds/);
  match(pageSource, /remainingVolcanoAssetLimits/);
  match(pageSource, /assetIdFromReferenceUrl\(item\.url\)/);
  match(pageSource, /appendVolcanoAssetReferences\(/);
  match(pageSource, /onUse=\{useVolcanoAssets\}/);
  match(pageSource, /onDeleted=\{removeDeletedVolcanoAssets\}/);
  match(pageSource, /removeReferencesAndReindexPrompt\(/);
  match(pageSource, /removeReferenceAndReindexPrompt\(/);
  match(pageSource, /onRemove=\{\(\) => removeReferenceDraft\(item\)\}/);
});

test("reference submit rejects audio-only media before calling the API", () => {
  match(
    pageSource,
    /referenceCounts\.image \+ referenceCounts\.video === 0/,
  );
  match(pageSource, /不能仅使用音频/);
});

test("video asset upload proxy budget exceeds the accepted file limit", () => {
  match(
    volcanoAssetDomainSource,
    /LUMEN_ASSET_VIDEO_MAX_BYTES = 64 \* 1024 \* 1024/,
  );
  match(nextConfigSource, /proxyClientMaxBodySize: "80mb"/);
});

test("video prompt enhancement respects Vibe Creating non-rewrite actions", () => {
  match(source, /type PromptEnhanceAction =/);
  match(source, /action === "ask_first"/);
  match(source, /action === "keep_original"/);
  match(source, /action === "optional_vc"/);
  match(source, /function shouldAutoApplyPromptEnhanceCandidate/);
  match(
    source,
    /candidate\.action === "direct_pass" \|\|\s*candidate\.action === "light_refine"/,
  );
  match(source, /function canApplyPromptEnhanceCandidate/);
  match(source, /未自动替换/);
  match(source, /仅查看/);
});

test("video duration selector follows selected model action and resolution", () => {
  match(
    source,
    /durations_by_action_resolution\?\.\[action\]\?\.\[resolution\]/,
  );
  match(source, /durations_by_action\?\.\[action\]/);
  match(optionsModelSource, /export function durationOrPreferred\(/);
  match(source, /setResolution\(nextResolution\)/);
  match(
    source,
    /setDurationS\(\(prev\) =>\s*durationOrPreferred\(prev, nextDurations\),\s*\)/,
  );
});

test("video temporary upstream download is available before local storage", () => {
  match(source, /export function activeVideoTemporaryDownload\(/);
  match(source, /expiresAtMs - nowMs/);
  match(source, /TEMPORARY_DOWNLOAD_MIN_REMAINING_MS/);
  match(source, /activeVideoTemporaryDownload\(item, nowMs\)/);
  match(source, /setNowMs\(Date\.now\(\)\)/);
  match(source, /target=\{isTemporary \? "_blank" : undefined\}/);
  match(source, /\{isTemporary \? "快速下载" : "下载"\}/);
});

test("video queries and polling cancel stale requests by task epoch", () => {
  match(source, /queryFn: \(\{ signal \}\) => fetchVideoOptions\(signal\)/);
  match(source, /queryFn: \(\{ pageParam, signal \}\) =>/);
  match(source, /fetchVideoGeneration\(id, request\.controller\.signal\)/);
  match(source, /currentEpoch === request\.epoch/);
  match(source, /generationRefreshRequestIsCurrent\(/);
  match(source, /existing\?\.controller\.abort\(\)/);
  match(source, /abortGenerationRefresh\(item\.id\)/);
  doesNotMatch(source, /queryFn: getVideoOptions/);
});

test("terminal video refresh failures preserve force sync and retry", () => {
  match(
    pageSource,
    /const scheduleGenerationRefreshRef = useRef<ScheduleGenerationRefresh>/,
  );
  match(
    pageSource,
    /recordGenerationRefreshFailure\([\s\S]*?if \(forceHistorySync\) \{\s*pendingHistoryRefreshRef\.current\.add\(id\);\s*\}[\s\S]*?scheduleGenerationRefreshRef\.current\(id, \{ forceHistorySync \}\)/,
  );
  match(
    pageSource,
    /scheduleGenerationRefreshRef\.current = scheduleGenerationRefresh/,
  );
  match(
    pageSource,
    /await qc\.invalidateQueries\(\{ queryKey: \["video", "generations"\] \}\);\s*if \(terminal\) terminalHistorySyncedRef\.current\.add\(id\)/,
  );
});

test("video retry fences the original request while accepting a new task id", () => {
  const retryStart = pageSource.indexOf("const retryMut = useMutation");
  const retryEnd = pageSource.indexOf("const deleteMut = useMutation");
  ok(retryStart >= 0 && retryEnd > retryStart);
  const retrySource = pageSource.slice(retryStart, retryEnd);

  match(
    retrySource,
    /mutationFn: \(request: VideoRequestFence\) =>\s*retryVideoGeneration\(request\.taskId\)/,
  );
  match(
    retrySource,
    /isVideoRequestFenceCurrent\(\s*retryRequestFenceRef\.current,\s*request/,
  );
  doesNotMatch(retrySource, /if \(gen\.id !== request\.taskId\) return/);
  match(retrySource, /setItems\(\(prev\) => mergeById\(prev, \[gen\]\)\)/);
  match(
    retrySource,
    /scheduleGenerationRefresh\(gen\.id, \{ delayMs: 800 \}\)/,
  );
  match(retrySource, /已创建新的重试任务/);
  match(retrySource, /正在跟踪新任务 \$\{gen\.id\.slice\(0, 8\)\}/);
  match(
    retrySource,
    /nextVideoRequestFence\(\s*retryRequestFenceRef\.current,\s*generationId/,
  );
  match(retrySource, /retryMut\.mutate\(request\)/);
});

test("video uploads are fenced to the current draft and upload epoch", () => {
  match(source, /type DraftUploadRequest =/);
  match(source, /draftFence: VideoRequestFence/);
  match(source, /isCurrentFirstFrameUpload/);
  match(source, /isCurrentReferenceUpload/);
  match(
    source,
    /isVideoRequestFenceCurrent\(draftFenceRef\.current, request\.draftFence\)/,
  );
  match(
    source,
    /uploadImage\(request\.file, \{ signal: request\.controller\.signal \}\)/,
  );
  match(
    source,
    /uploadReferenceVideo\(\s*request\.file,\s*request\.controller\.signal/,
  );
  match(source, /switchDraftContext\(item\.id, item\.action\)/);
  match(source, /cancelFirstFrameUpload\(\)/);
  match(source, /cancelReferenceUpload\(\)/);
  match(
    source,
    /if \(isAbortError\(err\) \|\| !isCurrentReferenceUpload\(request\)\) return/,
  );
});

test("video reference object URLs are revoked on replacement and unmount", () => {
  match(source, /function revokeUnusedReferenceObjectUrls\(/);
  match(source, /url\.startsWith\("blob:"\)/);
  match(source, /URL\.revokeObjectURL\(url\)/);
  match(
    source,
    /revokeUnusedReferenceObjectUrls\(\s*previousReferenceMediaRef\.current,\s*referenceMedia/,
  );
  match(
    source,
    /revokeUnusedReferenceObjectUrls\(\s*previousReferenceMediaRef\.current,\s*\[\]/,
  );
});

test("video prompt enhancement cannot write into a later draft", () => {
  match(source, /const requestEpoch = promptEnhanceEpochRef\.current \+ 1/);
  match(source, /const requestDraftFence = \{ \.\.\.draftFenceRef\.current \}/);
  match(source, /const isCurrentRequest = \(\) =>/);
  match(source, /promptEnhanceEpochRef\.current === requestEpoch/);
  match(
    source,
    /isVideoRequestFenceCurrent\(draftFenceRef\.current, requestDraftFence\)/,
  );
  match(
    source,
    /promptEnhanceAbortRef\.current === ctl &&\s*promptEnhanceEpochRef\.current === requestEpoch/,
  );
});

test("video dialogs guard focus and keyboard handling", () => {
  match(source, /export function focusVideoWorkbenchElement\(/);
  match(source, /export function isTopmostVideoDialog\(/);
  match(source, /export function trapVideoDialogFocus\(/);
  match(source, /export function restoreVideoWorkbenchFocus\(/);
  match(source, /if \(!isTopmostVideoDialog\(dialog\)\) return/);
  match(source, /focusPromptTarget\(target/);
  match(source, /tabIndex=\{-1\}/);
});

test("video upload state blocks submit and Enter shortcuts", () => {
  match(source, /if \(uploadPending\) return "等待素材上传完成"/);
  match(source, /const canSubmit = submitDisabledReason === "可以提交"/);
  match(source, /!event\.nativeEvent\.isComposing/);
  match(source, /!referenceUploadMut\.isPending/);
  match(source, /onSubmit=\{submitVideo\}/);
});

test("video task rows and preview show elapsed runtime", () => {
  match(
    source,
    /function formatTaskElapsed\(ms\?: number \| null\): string \| null/,
  );
  match(
    source,
    /function taskElapsedLabel\(item: VideoGenerationOut\): string \| null/,
  );
  match(
    source,
    /\$\{isTerminalVideo\(item\) \? "耗时" : "已耗时"\} \$\{elapsed\}/,
  );
  match(source, /const elapsedLabel = taskElapsedLabel\(item\)/);
  match(source, /\{elapsedLabel && <span>\{elapsedLabel\}<\/span>\}/);
});
