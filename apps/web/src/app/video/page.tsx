"use client";

/* eslint-disable @next/next/no-img-element -- Video posters are authenticated API media URLs. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  Clapperboard,
  CircleCheck,
  ClipboardList,
  Copy,
  Download,
  Film,
  FileText,
  Gauge,
  ImageIcon,
  Layers3,
  ListChecks,
  PencilLine,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  Settings2,
  Sparkles,
  Tags,
  Trash2,
  Upload,
  Video as VideoIcon,
  XCircle,
} from "lucide-react";
import { motion } from "framer-motion";

import {
  cancelVideoGeneration,
  createConversation,
  createVideoGeneration,
  deleteVideo,
  enhanceVideoPrompt,
  getTask,
  getVideoGeneration,
  getVideoOptions,
  imageVariantUrl,
  listMessages,
  listVideoGenerations,
  postMessage,
  retryVideoGeneration,
  uploadImage,
  uploadVideo,
  videoBinaryUrl,
  videoDownloadUrl,
  type BackendCompletion,
  type BackendGeneration,
  type BackendImageMeta,
} from "@/lib/apiClient";
import { prewarmImage, prewarmVideoMetadata } from "@/lib/imagePreload";
import { useSSE } from "@/lib/useSSE";
import type {
  VideoAction,
  VideoCreateIn,
  VideoGenerationOut,
  VideoOptionsOut,
  VideoReferenceMediaIn,
} from "@/lib/types";
import { Button, Card, toast } from "@/components/ui/primitives";
import { DesktopTopNav, MobileTabBar } from "@/components/ui/shell";
import { formatRmb } from "@/lib/money";
import { cn, uuid } from "@/lib/utils";

type VideoGenerationWithVideo = VideoGenerationOut & {
  video: NonNullable<VideoGenerationOut["video"]>;
};

type ReferenceDraft = VideoReferenceMediaIn & {
  _key: string;
  label: string;
  display: string;
};

type PromptEnhanceCandidate = {
  id: string;
  title: string;
  prompt: string;
};

type VideoWorkspaceMode = "storyboard" | "single";
type StoryboardWorkflowStage =
  | "idea"
  | "script"
  | "assets"
  | "shots"
  | "keyframes"
  | "videos";
type StoryboardAssetKind = "character" | "scene" | "prop";

type StoryboardShot = {
  id: string;
  title: string;
  purpose: string;
  durationS: number;
  narration: string;
  visual: string;
  shotType: string;
  cameraMove: string;
  transition: string;
  referenceNotes: string;
  assetIds: string[];
  approved: boolean;
  approvedAt?: string;
  keyframePrompt: string;
  keyframeGenerationId?: string;
  keyframeImageId?: string;
  keyframeImageUrl?: string;
  keyframeApproved: boolean;
  keyframeApprovedAt?: string;
  keyframeSourceHash?: string;
  generationId?: string;
  videoApproved: boolean;
  videoApprovedAt?: string;
};

type StoryboardShotPatch = Partial<Omit<StoryboardShot, "id">>;

type StoryboardReferenceAsset = {
  id: string;
  kind: StoryboardAssetKind;
  name: string;
  role: string;
  description: string;
  continuity: string;
  prompt: string;
  revision: number;
  approved: boolean;
  approvedAt?: string;
  generationId?: string;
  imageId?: string;
  imageUrl?: string;
};

type StoryboardReferenceAssetPatch = Partial<
  Omit<StoryboardReferenceAsset, "id" | "kind">
>;

const VIDEO_EVENTS = [
  "video.queued",
  "video.submitted",
  "video.progress",
  "video.fetching",
  "video.succeeded",
  "video.failed",
  "video.canceled",
];
const SMART_VIDEO_DURATION = -1;
const SMART_VIDEO_HOLD_DURATION = 15;
const VIDEO_DURATION_OPTIONS = [
  SMART_VIDEO_DURATION,
  ...Array.from({ length: 13 }, (_, index) => index + 3),
];
const VIDEO_RESOLUTION_VALUES = new Set<VideoCreateIn["resolution"]>([
  "480p",
  "720p",
  "1080p",
  "4k",
]);
const ACTIVE_VIDEO_STATUSES = ["queued", "submitting", "submitted", "running"] as const;
const TERMINAL_VIDEO_STATUSES = ["succeeded", "failed", "canceled", "expired"] as const;
const SETTLING_VIDEO_STAGES = ["fetching", "storing", "billing"] as const;
const VIDEO_ACTIVE_POLL_MS = 2500;
const VIDEO_REFRESH_MIN_INTERVAL_MS = 900;
const VIDEO_REFRESH_RETRY_BASE_MS = 1500;
const VIDEO_REFRESH_RETRY_MAX_MS = 15000;
const VIDEO_PROMPT_VARIANT_COUNT = 3;
const VIDEO_HISTORY_PAGE_SIZE = 12;
const VIDEO_PROMPT_VARIANT_TITLES = [
  "推荐镜头版",
  "动作节奏版",
  "参考一致版",
];

const STORYBOARD_MAX_GROUP_DURATION_S = 15;
const STORYBOARD_MIN_SHOT_DURATION_S = 3;
const STORYBOARD_MAX_SHOT_COUNT = 60;
const STORYBOARD_MAX_REFERENCE_IMAGES_PER_SHOT = 8;
const DEFAULT_STORYBOARD_STYLE =
  "真人电影感，清晰主体，统一角色外观，干净转场，镜头运动服务叙事，不添加分镜外的新剧情。";
const DEFAULT_STORYBOARD_SCRIPT =
  "开场用强钩子展示主角遇到的问题；随后用一个动作细节说明痛点；中段让产品或解决方案自然进入；结尾给出情绪释放和明确结果。";
const DEFAULT_STORYBOARD_IDEA =
  "一个 20 秒左右的产品短视频：用一个具体生活场景打开，展示用户遇到的问题，再让解决方案自然进入，最后给出清晰结果。";

const STORYBOARD_SHOT_TYPES = [
  "近景",
  "中景",
  "特写",
  "全景",
  "过肩",
  "手部特写",
  "低角度",
  "俯拍",
];

const STORYBOARD_CAMERA_MOVES = [
  "缓慢推镜",
  "稳定跟拍",
  "轻微横移",
  "静止留白",
  "焦点转换",
  "遮挡转场",
  "动作接动作",
  "轻微手持",
];

type VideoHistoryFilter = "all" | "succeeded" | "failed";

function nowIso(): string {
  return new Date().toISOString();
}

function storyboardAssetLabel(kind: StoryboardAssetKind): string {
  if (kind === "character") return "人物";
  if (kind === "scene") return "场景";
  return "道具";
}

function storyboardAssetDefaultName(kind: StoryboardAssetKind): string {
  if (kind === "character") return "主角";
  if (kind === "scene") return "主场景";
  return "关键道具";
}

function compactHash(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function clampStoryboardDuration(value: number): number {
  if (!Number.isFinite(value)) return 5;
  return Math.max(
    STORYBOARD_MIN_SHOT_DURATION_S,
    Math.min(STORYBOARD_MAX_GROUP_DURATION_S, Math.round(value)),
  );
}

function createStoryboardShot(input: Partial<StoryboardShot> = {}): StoryboardShot {
  return {
    id: uuid(),
    title: input.title ?? "新镜头",
    purpose: input.purpose ?? "推进叙事或补充动作信息",
    durationS: clampStoryboardDuration(input.durationS ?? 5),
    narration: input.narration ?? "",
    visual: input.visual ?? "描述这一镜头的画面主体、动作起点、动作峰值和落点。",
    shotType: input.shotType ?? "中景",
    cameraMove: input.cameraMove ?? "缓慢推镜",
    transition: input.transition ?? "动作转场",
    referenceNotes: input.referenceNotes ?? "",
    assetIds: input.assetIds ?? [],
    approved: input.approved ?? false,
    approvedAt: input.approvedAt,
    keyframePrompt: input.keyframePrompt ?? "",
    keyframeGenerationId: input.keyframeGenerationId,
    keyframeImageId: input.keyframeImageId,
    keyframeImageUrl: input.keyframeImageUrl,
    keyframeApproved: input.keyframeApproved ?? false,
    keyframeApprovedAt: input.keyframeApprovedAt,
    keyframeSourceHash: input.keyframeSourceHash,
    generationId: input.generationId,
    videoApproved: input.videoApproved ?? false,
    videoApprovedAt: input.videoApprovedAt,
  };
}

function createInitialStoryboardShots(): StoryboardShot[] {
  return [
    createStoryboardShot({
      title: "钩子开场",
      purpose: "用问题或反差让观众立刻进入故事。",
      durationS: 5,
      narration: "先把观众拉进主角的处境。",
      visual: "主角停在画面中心，环境里有一个清晰的冲突信号，镜头从细节推到人物反应。",
      shotType: "近景",
      cameraMove: "缓慢推镜",
      transition: "声音先入后切画面",
    }),
    createStoryboardShot({
      title: "痛点动作",
      purpose: "把问题变成可看见的动作和反应。",
      durationS: 6,
      narration: "问题不是被讲出来，而是被看见。",
      visual: "主角尝试完成动作但被阻断，手部、表情和道具形成连续动作。",
      shotType: "手部特写",
      cameraMove: "稳定跟拍",
      transition: "动作接动作",
    }),
    createStoryboardShot({
      title: "解决方案进入",
      purpose: "让产品、方法或关键转折自然出现。",
      durationS: 7,
      narration: "解决方案进入，但不要像硬广告。",
      visual: "关键物件从前景进入，主角视线被引导过去，环境光线变得更干净。",
      shotType: "过肩",
      cameraMove: "焦点转换",
      transition: "视线转场",
    }),
    createStoryboardShot({
      title: "结果释放",
      purpose: "给出完成感、情绪变化和下一步行动。",
      durationS: 6,
      narration: "结尾展示结果，让观众知道改变发生了。",
      visual: "主角完成动作并停留半秒，画面空间打开，结果物在清晰位置出现。",
      shotType: "全景",
      cameraMove: "轻微横移",
      transition: "硬切到收束画面",
    }),
  ];
}

function createStoryboardAsset(
  kind: StoryboardAssetKind,
  input: Partial<StoryboardReferenceAsset> = {},
): StoryboardReferenceAsset {
  const defaultDescription =
    kind === "character"
      ? "年龄、气质、发型、服装、关键配饰和表情基调。"
      : kind === "scene"
        ? "空间布局、时代感、材质、光线方向和可重复出现的道具。"
        : "产品或关键物件的外形、材质、颜色、比例和使用方式。";
  const defaultContinuity =
    kind === "character"
      ? "所有镜头保持同一张脸、同一发型、同一服装轮廓和配饰。"
      : kind === "scene"
        ? "所有镜头保持同一空间结构、光线方向、材质和关键道具位置。"
        : "所有镜头保持同一造型、品牌特征、材质、颜色和尺寸比例。";
  return {
    id: uuid(),
    kind,
    name: input.name ?? storyboardAssetDefaultName(kind),
    role:
      input.role ??
      (kind === "character"
        ? "故事主视角"
        : kind === "scene"
          ? "主要发生空间"
          : "叙事中的关键物件"),
    description: input.description ?? defaultDescription,
    continuity: input.continuity ?? defaultContinuity,
    prompt: input.prompt ?? "",
    revision: input.revision ?? 1,
    approved: input.approved ?? false,
    approvedAt: input.approvedAt,
    generationId: input.generationId,
    imageId: input.imageId,
    imageUrl: input.imageUrl,
  };
}

function createInitialStoryboardAssets(): StoryboardReferenceAsset[] {
  return [
    createStoryboardAsset("character", {
      name: "主角",
      role: "故事主视角",
      description:
        "年轻专业用户，干净自然的外观，现代休闲服装，表情从焦虑转为放松。",
      continuity:
        "保持同一张脸、发型、服装色块、身形比例和主要配饰；不同镜头只改变动作和表情。",
    }),
    createStoryboardAsset("scene", {
      name: "主要场景",
      role: "问题发生与结果展示的连续空间",
      description:
        "真实生活或工作空间，布局清楚，有能承载问题和解决方案的桌面、道具与背景层次。",
      continuity:
        "保持同一空间布局、光线方向、桌面材质、背景物位置和整体色温。",
    }),
    createStoryboardAsset("prop", {
      name: "关键道具",
      role: "解决方案或结果展示的视觉锚点",
      description:
        "外形简洁、轮廓清楚，材质和颜色稳定，在镜头中能被快速识别。",
      continuity:
        "保持同一外形、颜色、材质、尺寸比例和使用方式；不同镜头只改变摆放角度。",
    }),
  ];
}

function formatStoryboardAssetPrompt({
  asset,
  script,
  style,
}: {
  asset: StoryboardReferenceAsset;
  script: string;
  style: string;
}): string {
  const subject =
    asset.kind === "character"
      ? "人物一致性设定图，单人角色 identity board"
      : asset.kind === "scene"
        ? "场景一致性设定图，单一空间 environment reference"
        : "道具一致性设定图，单个产品/物件 prop reference";
  return [
    `请生成一张${subject}。`,
    "用途：后续每个分镜关键帧都会参考这张图来保持连续性。",
    `名称：${asset.name}`,
    `叙事角色：${asset.role}`,
    `外观/空间描述：${asset.description}`,
    `一致性约束：${asset.continuity}`,
    `项目脚本：${script || DEFAULT_STORYBOARD_SCRIPT}`,
    `项目视觉风格：${style || DEFAULT_STORYBOARD_STYLE}`,
    asset.kind === "character"
      ? "画面要求：同一角色的正面或三分之二角度，完整头肩到半身，服装和配饰清晰，背景干净，不要多人物，不要文字标注。"
      : asset.kind === "scene"
        ? "画面要求：单一可复用场景，空间结构清楚，主要道具和光线方向清晰，不要人物，不要文字标注。"
        : "画面要求：单个道具或产品居中展示，外形、材质、颜色和比例清晰，背景干净，不要人物，不要文字标注。",
    "禁止：不要加入 logo、水印、UI、分镜编号、漫画格或任何屏幕文字。",
  ].join("\n");
}

function formatScriptExpansionPrompt({
  idea,
  title,
  style,
}: {
  idea: string;
  title: string;
  style: string;
}): string {
  return [
    "把用户的视频想法扩写成可确认的短视频脚本。",
    "输出只要中文脚本正文，不要解释，不要 Markdown。",
    "要求：20-45 秒时通常拆成 4-8 个连续段落；更复杂的脚本可以更多段。每段描述一个可拍的动作/情绪/信息变化；保留人物、场景、产品或关键物件的一致性线索；不要写空泛营销话术。",
    `项目名：${title || "Lumen 分镜项目"}`,
    `想法：${idea || DEFAULT_STORYBOARD_IDEA}`,
    `视觉风格：${style || DEFAULT_STORYBOARD_STYLE}`,
  ].join("\n");
}

function formatAssetExtractionPrompt(script: string): string {
  return [
    "从下面脚本中提取后续分镜需要保持一致的人物、场景和关键道具设定。",
    "只输出 JSON，不要解释，不要 Markdown。",
    "JSON 格式：",
    '{"characters":[{"name":"主角","role":"叙事角色","description":"外观、服装、配饰、气质","continuity":"跨镜头必须保持一致的点"}],"scenes":[{"name":"主要场景","role":"剧情用途","description":"空间布局、光线、材质、关键道具","continuity":"跨镜头必须保持一致的点"}],"props":[{"name":"关键道具","role":"剧情用途","description":"外形、材质、颜色、比例","continuity":"跨镜头必须保持一致的点"}]}',
    `脚本：${script || DEFAULT_STORYBOARD_SCRIPT}`,
  ].join("\n");
}

function parseJsonObject(text: string): Record<string, unknown> | null {
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1];
  const source = fenced ?? text;
  const start = source.indexOf("{");
  const end = source.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  try {
    const parsed = JSON.parse(source.slice(start, end + 1));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function stringFromRecord(
  value: unknown,
  key: string,
  fallback: string,
): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return fallback;
  const record = value as Record<string, unknown>;
  const raw = record[key];
  return typeof raw === "string" && raw.trim() ? raw.trim() : fallback;
}

function parseAssetsFromAiText(text: string): StoryboardReferenceAsset[] {
  const parsed = parseJsonObject(text);
  if (!parsed) return [];
  const characters = Array.isArray(parsed.characters) ? parsed.characters : [];
  const scenes = Array.isArray(parsed.scenes) ? parsed.scenes : [];
  const props = Array.isArray(parsed.props) ? parsed.props : [];
  const characterAssets = characters.slice(0, 4).map((item, index) =>
    createStoryboardAsset("character", {
      name: stringFromRecord(item, "name", index === 0 ? "主角" : `人物 ${index + 1}`),
      role: stringFromRecord(item, "role", "故事角色"),
      description: stringFromRecord(item, "description", "人物外观与服装设定"),
      continuity: stringFromRecord(item, "continuity", "保持人物身份、服装和气质一致"),
    }),
  );
  const sceneAssets = scenes.slice(0, 4).map((item, index) =>
    createStoryboardAsset("scene", {
      name: stringFromRecord(item, "name", index === 0 ? "主要场景" : `场景 ${index + 1}`),
      role: stringFromRecord(item, "role", "剧情空间"),
      description: stringFromRecord(item, "description", "空间布局、光线和道具设定"),
      continuity: stringFromRecord(item, "continuity", "保持空间结构、光线和道具一致"),
    }),
  );
  const propAssets = props.slice(0, 4).map((item, index) =>
    createStoryboardAsset("prop", {
      name: stringFromRecord(item, "name", index === 0 ? "关键道具" : `道具 ${index + 1}`),
      role: stringFromRecord(item, "role", "剧情物件"),
      description: stringFromRecord(item, "description", "道具外形、材质和颜色设定"),
      continuity: stringFromRecord(item, "continuity", "保持道具造型、材质和比例一致"),
    }),
  );
  return [...characterAssets, ...sceneAssets, ...propAssets];
}

function splitStoryboardScript(script: string): string[] {
  const normalized = script
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const rawParts =
    normalized.length >= 3
      ? normalized
      : script
          .replace(/\r\n/g, "\n")
          .split(/(?<=[。！？!?；;])|\n+/)
          .map((part) => part.trim())
          .filter(Boolean);
  return rawParts.slice(0, STORYBOARD_MAX_SHOT_COUNT);
}

function titleFromScriptPart(part: string, index: number): string {
  const cleaned = part.replace(/[，。！？；：,.!?;:]/g, " ").trim();
  const first = cleaned.split(/\s+/).filter(Boolean).join("");
  return first ? first.slice(0, 12) : `镜头 ${index + 1}`;
}

function estimateShotDuration(part: string, index: number): number {
  const chineseLikeLength = Array.from(part.trim()).length;
  const base = Math.ceil(chineseLikeLength / 18) + 3;
  const rhythm = index % 3 === 0 ? 1 : 0;
  return clampStoryboardDuration(base + rhythm);
}

function createShotsFromScript(script: string): StoryboardShot[] {
  const parts = splitStoryboardScript(script);
  if (parts.length === 0) return [];
  return parts.map((part, index) =>
    createStoryboardShot({
      title: titleFromScriptPart(part, index),
      purpose:
        index === 0
          ? "开场建立钩子和处境。"
          : index === parts.length - 1
            ? "收束结果，保留情绪停顿。"
            : "推进动作、信息或情绪转折。",
      durationS: estimateShotDuration(part, index),
      narration: part,
      visual: `${part} 用一个清楚的动作瞬间表达，不依赖屏幕文字说明。`,
      shotType: STORYBOARD_SHOT_TYPES[index % STORYBOARD_SHOT_TYPES.length],
      cameraMove: STORYBOARD_CAMERA_MOVES[index % STORYBOARD_CAMERA_MOVES.length],
      transition:
        index === 0
          ? "声音先入"
          : index === parts.length - 1
            ? "节奏停顿后收束"
            : "动作接动作",
    }),
  );
}

function bindAllStoryboardAssetIds(assets: StoryboardReferenceAsset[]): string[] {
  return assets.map((asset) => asset.id);
}

function assetIdsForShot(
  shot: StoryboardShot,
  assets: StoryboardReferenceAsset[],
): string[] {
  const existingIds = new Set(assets.map((asset) => asset.id));
  const bound = shot.assetIds.filter((id) => existingIds.has(id));
  return bound.length > 0 ? bound : bindAllStoryboardAssetIds(assets);
}

function assetsForShot(
  shot: StoryboardShot,
  assets: StoryboardReferenceAsset[],
): StoryboardReferenceAsset[] {
  const ids = new Set(assetIdsForShot(shot, assets));
  return assets.filter((asset) => ids.has(asset.id));
}

function approvedImageIdsForShot(
  shot: StoryboardShot,
  assets: StoryboardReferenceAsset[],
): string[] {
  return assetsForShot(shot, assets)
    .filter((asset) => asset.approved && asset.imageId)
    .map((asset) => asset.imageId)
    .filter((id): id is string => Boolean(id))
    .slice(0, STORYBOARD_MAX_REFERENCE_IMAGES_PER_SHOT);
}

function storyboardShotSourceHash(
  shot: StoryboardShot,
  assets: StoryboardReferenceAsset[],
  extraImageIds: string[] = [],
): string {
  const sourceAssets = assetsForShot(shot, assets)
    .map(
      (asset) =>
        `${asset.id}:${asset.kind}:${asset.revision}:${asset.approved ? "approved" : "draft"}`,
    )
    .sort()
    .join("|");
  return compactHash(
    [
      shot.title,
      shot.purpose,
      shot.durationS,
      shot.narration,
      shot.visual,
      shot.shotType,
      shot.cameraMove,
      shot.transition,
      shot.referenceNotes,
      shot.keyframePrompt,
      sourceAssets,
      extraImageIds.slice().sort().join("|"),
    ].join("\n"),
  );
}

function storyboardShotKeyframeStale(
  shot: StoryboardShot,
  assets: StoryboardReferenceAsset[],
  extraImageIds: string[] = [],
): boolean {
  if (!shot.keyframeImageId || !shot.keyframeSourceHash) return false;
  return (
    shot.keyframeSourceHash !==
    storyboardShotSourceHash(shot, assets, extraImageIds)
  );
}

function formatStoryboardShotPrompt({
  projectTitle,
  projectStyle,
  shot,
  shotNumber,
  totalShots,
  hasReferences,
}: {
  projectTitle: string;
  projectStyle: string;
  shot: StoryboardShot;
  shotNumber: number;
  totalShots: number;
  hasReferences: boolean;
}): string {
  const referenceInstruction = hasReferences
    ? "参考素材用于保持角色、产品、场景和风格一致；不要机械复刻参考素材中的文字、边框、标注或 UI。"
    : "无参考素材时，按本镜头描述建立清晰主体和稳定视觉风格。";
  return [
    `项目：${projectTitle || "Lumen 分镜项目"}`,
    `片段：SHOT ${String(shotNumber).padStart(2, "0")} / ${totalShots} - ${shot.title}`,
    `时长：${shot.durationS} 秒，生成为可独立剪辑的视频片段。`,
    `视觉风格：${projectStyle || DEFAULT_STORYBOARD_STYLE}`,
    `镜头目的：${shot.purpose}`,
    `画面内容：${shot.visual}`,
    `景别与机位：${shot.shotType}`,
    `镜头运动：${shot.cameraMove}`,
    `转场方式：${shot.transition}`,
    `台词/旁白/字幕信息：${shot.narration || "无对白，用动作和情绪推进。"}`,
    `参考说明：${shot.referenceNotes || referenceInstruction}`,
    "首帧约束：输入图片是本镜头已确认的分镜关键帧，必须保持人物身份、服装、场景结构、主体位置和光线方向。",
    "生成要求：主体动作要有起点、峰值和落点；镜头运动必须服务叙事；保持角色、服装、道具、光线方向和空间关系连续；不要添加新角色、新剧情、logo、水印或额外屏幕文字。",
  ].join("\n");
}

function formatStoryboardKeyframePrompt({
  projectTitle,
  projectStyle,
  shot,
  shotNumber,
  totalShots,
  hasReferences,
}: {
  projectTitle: string;
  projectStyle: string;
  shot: StoryboardShot;
  shotNumber: number;
  totalShots: number;
  hasReferences: boolean;
}): string {
  const referenceInstruction = hasReferences
    ? "参考素材只用于保持角色、产品、服装、材质和场景风格一致。"
    : "无参考素材时，按项目视觉风格建立清晰、可复用的角色和场景。";
  return [
    "请生成一张单镜头最终效果画面图（single-shot cinematic keyframe reference）。",
    "这不是 storyboard sheet，不是漫画格，不是多格拼图，不要出现分镜边框、编号、箭头、标注、字幕、logo、水印或 UI。",
    `项目：${projectTitle || "Lumen 分镜项目"}`,
    `镜头：SHOT ${String(shotNumber).padStart(2, "0")} / ${totalShots} - ${shot.title}`,
    `最终视觉风格：${projectStyle || DEFAULT_STORYBOARD_STYLE}`,
    `镜头目的：${shot.purpose}`,
    `画面内容：${shot.visual}`,
    `景别与机位：${shot.shotType}`,
    `镜头运动意图：${shot.cameraMove}`,
    `转场/节奏意图：${shot.transition}`,
    `台词/旁白对应瞬间：${shot.narration || "无对白，用动作和情绪推进。"}`,
    `参考素材说明：${shot.referenceNotes || referenceInstruction}`,
    "画面要求：16:9 横版，电影感构图，主体动作有清晰起点、峰值和落点；角色、服装、道具、光线方向、空间关系需要能和相邻镜头连续；画面应可直接作为图生视频首帧使用。",
    "禁止事项：不要新增分镜外的新角色、新剧情、新道具；不要改变已建立的角色外观、服装或场景逻辑；不要加入任何屏幕文字。",
  ].join("\n");
}

function mergeImageTasks(
  current: BackendGeneration[],
  updates: BackendGeneration[],
): BackendGeneration[] {
  const map = new Map(current.map((item) => [item.id, item]));
  for (const item of updates) map.set(item.id, item);
  return Array.from(map.values()).sort(
    (a, b) =>
      new Date(b.created_at ?? 0).getTime() -
      new Date(a.created_at ?? 0).getTime(),
  );
}

function isActiveImageTask(item: BackendGeneration): boolean {
  return item.status === "queued" || item.status === "running";
}

function isTerminalImageTask(item: BackendGeneration): boolean {
  return item.status === "succeeded" || item.status === "failed" || item.status === "canceled";
}

function isTerminalCompletionTask(item: BackendCompletion): boolean {
  return item.status === "succeeded" || item.status === "failed" || item.status === "canceled";
}

function imageTaskProgress(item: BackendGeneration | undefined): number {
  if (!item) return 0;
  if (item.status === "succeeded") return 100;
  if (item.status === "failed" || item.status === "canceled") return 0;
  if (item.progress_stage === "rendering") return 62;
  if (item.progress_stage === "finalizing") return 86;
  if (item.progress_stage === "understanding") return 28;
  return 8;
}

function imagePreviewUrl(image: BackendImageMeta): string {
  return (
    image.thumb_url ||
    image.preview_url ||
    image.display_url ||
    image.url ||
    imageVariantUrl(image.id, "preview1024")
  );
}

function storyboardImageAspect(value: string): "16:9" | "9:16" | "1:1" | "21:9" | "4:5" | "3:4" | "4:3" | "3:2" | "2:3" {
  if (
    value === "9:16" ||
    value === "1:1" ||
    value === "21:9" ||
    value === "4:5" ||
    value === "3:4" ||
    value === "4:3" ||
    value === "3:2" ||
    value === "2:3"
  ) {
    return value;
  }
  return "16:9";
}

async function waitForStoryboardCompletion(id: string): Promise<BackendCompletion> {
  for (let attempt = 0; attempt < 90; attempt += 1) {
    const task = await getTask("completions", id);
    if (isTerminalCompletionTask(task)) return task;
    await new Promise((resolve) => window.setTimeout(resolve, 1200));
  }
  throw new Error("AI 文本任务超时");
}

async function waitForStoryboardImageTask(id: string): Promise<BackendGeneration> {
  for (let attempt = 0; attempt < 150; attempt += 1) {
    const task = await getTask("generations", id);
    if (isTerminalImageTask(task)) return task;
    await new Promise((resolve) => window.setTimeout(resolve, 1400));
  }
  throw new Error("图片任务超时");
}

async function findGeneratedImage(
  conversationId: string,
  generationId: string,
): Promise<BackendImageMeta | null> {
  const messages = await listMessages(conversationId, {
    include: ["tasks"],
    limit: 80,
  });
  return (
    messages.images?.find((image) => image.owner_generation_id === generationId) ??
    null
  );
}

const MODE_COPY: Record<
  VideoAction,
  {
    title: string;
    eyebrow: string;
    description: string;
    requirement: string;
  }
> = {
  t2v: {
    title: "文字生成",
    eyebrow: "无参考素材",
    description: "只根据描述生成视频。",
    requirement: "填写描述",
  },
  i2v: {
    title: "首帧生成",
    eyebrow: "从图片开始",
    description: "用一张图片确定第一帧和构图。",
    requirement: "上传首帧",
  },
  reference: {
    title: "参考生成",
    eyebrow: "参考图片/视频",
    description: "用素材约束人物、物体或风格。",
    requirement: "添加素材",
  },
};

const PROMPT_CHIPS = [
  "近景",
  "推镜",
  "跟拍",
  "侧光",
  "转台",
  "干净背景",
  "浅景深",
  "轻微运动模糊",
];

const STAGE_COPY: Record<
  string,
  {
    label: string;
    detail: string;
  }
> = {
  queued: {
    label: "排队中",
    detail: "等待开始。",
  },
  submitting: {
    label: "提交中",
    detail: "正在提交。",
  },
  submitted: {
    label: "已提交",
    detail: "等待处理。",
  },
  rendering: {
    label: "生成中",
    detail: "正在生成。",
  },
  running: {
    label: "生成中",
    detail: "正在生成。",
  },
  fetching: {
    label: "取回结果",
    detail: "正在取回文件。",
  },
  storing: {
    label: "保存中",
    detail: "正在保存。",
  },
  billing: {
    label: "结算中",
    detail: "正在结算。",
  },
  finished: {
    label: "已完成",
    detail: "已保存。",
  },
  succeeded: {
    label: "已完成",
    detail: "已保存。",
  },
  failed: {
    label: "失败",
    detail: "失败，可重试。",
  },
  canceled: {
    label: "已取消",
    detail: "已取消。",
  },
  expired: {
    label: "已过期",
    detail: "已过期。",
  },
};

function holdEstimateDurationS(durationS: number): number {
  return durationS === SMART_VIDEO_DURATION ? SMART_VIDEO_HOLD_DURATION : durationS;
}

function formatDurationLabel(durationS: number): string {
  return durationS === SMART_VIDEO_DURATION ? "自动时长" : `${durationS}s`;
}

function isActiveVideo(item: VideoGenerationOut): boolean {
  if (ACTIVE_VIDEO_STATUSES.includes(
    item.status as (typeof ACTIVE_VIDEO_STATUSES)[number],
  )) {
    return true;
  }
  if (item.status === "succeeded" && !item.video) return true;
  return SETTLING_VIDEO_STAGES.includes(
    item.progress_stage as (typeof SETTLING_VIDEO_STAGES)[number],
  );
}

function isTerminalVideo(item: VideoGenerationOut): boolean {
  return TERMINAL_VIDEO_STATUSES.includes(
    item.status as (typeof TERMINAL_VIDEO_STATUSES)[number],
  );
}

function isTerminalVideoStatus(status: string | undefined): boolean {
  return TERMINAL_VIDEO_STATUSES.includes(
    status as (typeof TERMINAL_VIDEO_STATUSES)[number],
  );
}

function isFailedHistoryVideo(item: VideoGenerationOut): boolean {
  return ["failed", "canceled", "expired"].includes(item.status);
}

function videoHistoryFilterLabel(filter: VideoHistoryFilter): string {
  if (filter === "succeeded") return "成功";
  if (filter === "failed") return "失败";
  return "全部";
}

function actionLabel(action: VideoAction): string {
  return MODE_COPY[action]?.title ?? action.toUpperCase();
}

function stageCopy(item: VideoGenerationOut): { label: string; detail: string } {
  return (
    STAGE_COPY[item.progress_stage] ??
    STAGE_COPY[item.status] ?? {
      label: item.status,
      detail: item.progress_stage,
    }
  );
}

function progressForItem(item: VideoGenerationOut): number {
  if (item.status === "succeeded") return 100;
  if (["failed", "canceled", "expired"].includes(item.status)) {
    return Math.max(0, Math.min(100, item.progress_pct || 0));
  }
  return Math.max(4, Math.min(98, item.progress_pct || 0));
}

function toVideoResolution(value: string): VideoCreateIn["resolution"] {
  return VIDEO_RESOLUTION_VALUES.has(value as VideoCreateIn["resolution"])
    ? (value as VideoCreateIn["resolution"])
    : "720p";
}

function parseSeed(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function firstModelForAction(options: VideoOptionsOut | undefined, action: VideoAction): string {
  return options?.models.find((item) => item.actions.includes(action))?.model ?? "";
}

function resolutionOptionsForModel(
  options: VideoOptionsOut | undefined,
  model: string,
): string[] {
  const modelOptions = options?.models.find((item) => item.model === model);
  if (modelOptions?.resolutions?.length) return modelOptions.resolutions;
  return options?.resolutions?.length ? options.resolutions : ["480p", "720p", "1080p"];
}

function billingModelForAction(
  options: VideoOptionsOut | undefined,
  model: string,
  action: VideoAction,
): string {
  const modelOptions = options?.models.find((item) => item.model === model);
  const actionBillingModel = modelOptions?.billing_models?.[action]?.trim();
  if (actionBillingModel) return actionBillingModel;
  const billingModel = modelOptions?.billing_model?.trim();
  return billingModel || model;
}

function preferredResolution(options: string[]): string {
  return options.includes("720p") ? "720p" : options[0] ?? "720p";
}

function mergeById(
  current: VideoGenerationOut[],
  updates: VideoGenerationOut[],
): VideoGenerationOut[] {
  const map = new Map(current.map((item) => [item.id, item]));
  for (const item of updates) map.set(item.id, item);
  return Array.from(map.values()).sort(
    (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
  );
}

function estimateHoldMicro(
  options: VideoOptionsOut | undefined,
  {
    model,
    billingModel,
    action,
    resolution,
    durationS,
    referenceHasVideo,
  }: {
    model: string;
    billingModel?: string;
    action: VideoAction;
    resolution: string;
    durationS: number;
    referenceHasVideo?: boolean;
  },
): { tokens: number; micro: number } | null {
  const modelCandidates = Array.from(
    new Set([billingModel, model].filter(Boolean) as string[]),
  );
  const estimateActions =
    action === "reference"
      ? referenceHasVideo
        ? ["reference_video"]
        : ["reference_image", "reference", "i2v", "t2v"]
      : [action];
  const estimateKey = `${resolution}:${holdEstimateDurationS(durationS)}`;
  let tokensRaw: unknown;
  for (const modelCandidate of modelCandidates) {
    const tokenMap = options?.hold_estimates?.[modelCandidate];
    if (!tokenMap || typeof tokenMap !== "object") continue;
    const tokenRecord = tokenMap as Record<string, unknown>;
    for (const estimateAction of estimateActions) {
      const actionMap = tokenRecord[estimateAction];
      if (!actionMap || typeof actionMap !== "object") continue;
      tokensRaw = (actionMap as Record<string, unknown>)[estimateKey];
      if (tokensRaw != null) break;
    }
    if (tokensRaw != null) break;
  }
  const tokens = Number(tokensRaw);
  if (!Number.isFinite(tokens) || tokens <= 0) return null;
  const pricingAction =
    action === "reference"
      ? referenceHasVideo
        ? "reference_video"
        : "reference_image"
      : action;
  const findPrice = (priceAction: VideoAction | "reference_image" | "reference_video") => {
    for (const modelCandidate of modelCandidates) {
      const price =
        options?.pricing.find(
          (item) =>
            item.model === modelCandidate &&
            item.action === priceAction &&
            item.resolution === resolution &&
            item.enabled,
        ) ??
        options?.pricing.find(
          (item) =>
            item.model === modelCandidate &&
            item.action === priceAction &&
            (item.resolution == null || item.resolution === "") &&
            item.enabled,
        );
      if (price) return price;
    }
    return undefined;
  };
  const price =
    findPrice(pricingAction) ??
    (action === "reference" ? findPrice("reference") : undefined) ??
    (action === "reference" && !referenceHasVideo ? findPrice("i2v") : undefined);
  if (!price) return { tokens, micro: 0 };
  return { tokens, micro: Math.round((tokens * price.price.micro) / 1_000_000) };
}

function videoSrc(video: VideoGenerationWithVideo["video"]): string {
  return video.url?.trim() || videoBinaryUrl(video.id);
}

function videoDownloadSrc(id: string): string {
  return videoDownloadUrl(id);
}

function posterSrc(video: VideoGenerationWithVideo["video"]): string | undefined {
  return video.poster_url?.trim() || undefined;
}

function prewarmVideoItem(item: VideoGenerationWithVideo | null | undefined): void {
  if (!item) return;
  prewarmImage(posterSrc(item.video));
  prewarmVideoMetadata(videoSrc(item.video));
}

function hasVideo(item: VideoGenerationOut): item is VideoGenerationWithVideo {
  return item.video != null;
}

function videoDownloadName(item: VideoGenerationWithVideo): string {
  const ext = item.video.mime === "video/quicktime" ? "mov" : "mp4";
  return `lumen-video-${item.id.slice(0, 8)}.${ext}`;
}

function cleanPromptEnhanceText(value: string): string {
  return value
    .replace(/\r\n/g, "\n")
    .replace(/^```(?:json|text)?\s*/i, "")
    .replace(/\s*```$/i, "")
    .replace(/^(?:提示词|prompt)\s*[:：]\s*/i, "")
    .trim()
    .replace(/^["“]|["”]$/g, "")
    .trim();
}

function parsePromptEnhanceCandidates(raw: string): PromptEnhanceCandidate[] {
  const normalized = raw.replace(/\r\n/g, "\n").trim();
  if (!normalized) return [];
  const candidates: PromptEnhanceCandidate[] = [];
  const variantPattern =
    /<variant(?:\s+title=(?:"([^"]+)"|'([^']+)'))?\s*>([\s\S]*?)<\/variant>/gi;
  for (const match of normalized.matchAll(variantPattern)) {
    const promptText = cleanPromptEnhanceText(match[3] ?? "");
    if (!promptText) continue;
    const title =
      cleanPromptEnhanceText(match[1] ?? match[2] ?? "") ||
      VIDEO_PROMPT_VARIANT_TITLES[candidates.length] ||
      `方案 ${candidates.length + 1}`;
    candidates.push({
      id: `variant-${candidates.length + 1}`,
      title,
      prompt: promptText,
    });
  }
  if (candidates.length > 0) return candidates.slice(0, VIDEO_PROMPT_VARIANT_COUNT);
  const fallback = cleanPromptEnhanceText(normalized);
  return fallback ? [{ id: "variant-1", title: "优化结果", prompt: fallback }] : [];
}

function normalizeAssetUrl(value: string): string {
  const raw = value.trim().replace(/^["'`“”‘’]+|["'`“”‘’]+$/g, "").trim();
  if (!raw) return "";
  const stripped = raw.replace(/^asset\s*:\s*\/\s*\//i, "");
  const assetId = stripped.replace(/^[/\\]+/, "").trim();
  return assetId ? `asset://${assetId.toLowerCase()}` : "";
}

function referenceMediaPayload(item: ReferenceDraft): VideoReferenceMediaIn {
  if (item.url) {
    return {
      kind: item.kind,
      url: item.url,
      label: item.label,
    };
  }
  return {
    kind: item.kind,
    image_id: item.kind === "image" ? item.image_id ?? null : null,
    video_id: item.kind === "video" ? item.video_id ?? null : null,
    label: item.label,
  };
}

function storyboardReferenceImageIds(referenceMedia: ReferenceDraft[]): string[] {
  return referenceMedia
    .filter((item) => item.kind === "image" && item.image_id)
    .map((item) => item.image_id)
    .filter((id): id is string => Boolean(id));
}

function mergeStoryboardReferenceImageIds(
  primaryIds: string[],
  extraIds: string[],
): string[] {
  return Array.from(new Set([...primaryIds, ...extraIds])).slice(
    0,
    STORYBOARD_MAX_REFERENCE_IMAGES_PER_SHOT,
  );
}

function appendReferenceNote(current: string, label: string): string {
  const tag = `[${label}]`;
  if (current.includes(tag)) return current;
  return [current.trim(), tag].filter(Boolean).join(" ");
}

export default function VideoPage() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const referenceFileRef = useRef<HTMLInputElement | null>(null);
  const promptRef = useRef<HTMLTextAreaElement | null>(null);
  const promptEnhanceAbortRef = useRef<AbortController | null>(null);
  const terminalHistorySyncedRef = useRef<Set<string>>(new Set());
  const refreshInFlightRef = useRef<Set<string>>(new Set());
  const scheduledRefreshTimersRef = useRef<Map<string, number>>(new Map());
  const pendingHistoryRefreshRef = useRef<Set<string>>(new Set());
  const lastRefreshAtRef = useRef<Map<string, number>>(new Map());
  const refreshBackoffUntilRef = useRef<Map<string, number>>(new Map());
  const refreshFailureCountRef = useRef<Map<string, number>>(new Map());
  const [workspaceMode, setWorkspaceMode] = useState<VideoWorkspaceMode>("single");
  const [storyboardStage, setStoryboardStage] =
    useState<StoryboardWorkflowStage>("idea");
  const [storyboardTitle, setStoryboardTitle] = useState("Lumen 分镜视频项目");
  const [storyboardIdea, setStoryboardIdea] = useState(DEFAULT_STORYBOARD_IDEA);
  const [storyboardScript, setStoryboardScript] = useState(DEFAULT_STORYBOARD_SCRIPT);
  const [scriptConfirmed, setScriptConfirmed] = useState(false);
  const [scriptRevision, setScriptRevision] = useState(1);
  const [scriptApprovedRevision, setScriptApprovedRevision] = useState(0);
  const [scriptApprovedAt, setScriptApprovedAt] = useState("");
  const [storyboardStyle, setStoryboardStyle] = useState(DEFAULT_STORYBOARD_STYLE);
  const [storyboardAssets, setStoryboardAssets] = useState<
    StoryboardReferenceAsset[]
  >(() => createInitialStoryboardAssets());
  const [storyboardShots, setStoryboardShots] = useState<StoryboardShot[]>(
    () => createInitialStoryboardShots(),
  );
  const [selectedShotId, setSelectedShotId] = useState(() => storyboardShots[0]?.id ?? "");
  const [isSubmittingStoryboard, setIsSubmittingStoryboard] = useState(false);
  const [storyboardConversationId, setStoryboardConversationId] = useState("");
  const [isExpandingScript, setIsExpandingScript] = useState(false);
  const [isExtractingAssets, setIsExtractingAssets] = useState(false);
  const [storyboardImageTasks, setStoryboardImageTasks] = useState<
    BackendGeneration[]
  >([]);
  const [generatingAssetId, setGeneratingAssetId] = useState("");
  const [generatingKeyframeShotId, setGeneratingKeyframeShotId] = useState("");
  const [action, setAction] = useState<VideoAction>("t2v");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [durationS, setDurationS] = useState(5);
  const [resolution, setResolution] = useState("720p");
  const [aspectRatio, setAspectRatio] = useState("adaptive");
  const [generateAudio, setGenerateAudio] = useState(true);
  const [seed, setSeed] = useState("");
  const [inputImageId, setInputImageId] = useState("");
  const [uploadedLabel, setUploadedLabel] = useState("");
  const [referenceMedia, setReferenceMedia] = useState<ReferenceDraft[]>([]);
  const [assetUrlInput, setAssetUrlInput] = useState("");
  const [items, setItems] = useState<VideoGenerationOut[]>([]);
  const [selectedVideoId, setSelectedVideoId] = useState("");
  const [isEnhancingPrompt, setIsEnhancingPrompt] = useState(false);
  const [promptEnhancePreview, setPromptEnhancePreview] = useState("");
  const [promptEnhanceCandidates, setPromptEnhanceCandidates] = useState<
    PromptEnhanceCandidate[]
  >([]);
  const [selectedPromptEnhanceCandidateId, setSelectedPromptEnhanceCandidateId] =
    useState("");
  const [historyFilter, setHistoryFilter] = useState<VideoHistoryFilter>("all");

  const optionsQ = useQuery({
    queryKey: ["video", "options"],
    queryFn: getVideoOptions,
    retry: false,
    staleTime: 60_000,
    gcTime: 5 * 60_000,
  });
  const historyQ = useInfiniteQuery({
    queryKey: ["video", "generations"],
    queryFn: ({ pageParam }) =>
      listVideoGenerations({
        cursor: pageParam,
        limit: VIDEO_HISTORY_PAGE_SIZE,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: false,
    staleTime: 20_000,
    gcTime: 5 * 60_000,
  });
  const historyItems = useMemo(
    () => historyQ.data?.pages.flatMap((page) => page.items) ?? [],
    [historyQ.data?.pages],
  );

  const options = optionsQ.data;
  const effectiveItems = useMemo(
    () => mergeById(historyItems, items),
    [historyItems, items],
  );
  const activeItems = useMemo(
    () => effectiveItems.filter(isActiveVideo),
    [effectiveItems],
  );
  const generationById = useMemo(
    () => new Map(effectiveItems.map((item) => [item.id, item])),
    [effectiveItems],
  );
  const storyboardGeneratedCount = useMemo(
    () =>
      storyboardShots.filter((shot) => {
        const generation = shot.generationId
          ? generationById.get(shot.generationId)
          : undefined;
        return generation?.status === "succeeded" && Boolean(generation.video);
      }).length,
    [generationById, storyboardShots],
  );
  const selectedShot = useMemo(
    () => storyboardShots.find((shot) => shot.id === selectedShotId) ?? storyboardShots[0],
    [selectedShotId, storyboardShots],
  );
  const storyboardImageTaskById = useMemo(
    () => new Map(storyboardImageTasks.map((item) => [item.id, item])),
    [storyboardImageTasks],
  );
  const storyboardExternalReferenceImageIds = useMemo(
    () => storyboardReferenceImageIds(referenceMedia),
    [referenceMedia],
  );
  const storyboardAssetReadyCount = storyboardAssets.filter(
    (asset) => asset.imageId,
  ).length;
  const storyboardAssetApprovedCount = storyboardAssets.filter(
    (asset) => asset.approved && asset.imageId,
  ).length;
  const storyboardKeyframeReadyCount = storyboardShots.filter(
    (shot) => shot.keyframeImageId,
  ).length;
  const storyboardKeyframeApprovedCount = storyboardShots.filter(
    (shot) =>
      shot.keyframeApproved &&
      !storyboardShotKeyframeStale(
        shot,
        storyboardAssets,
        storyboardExternalReferenceImageIds,
      ),
  ).length;
  const storyboardStaleKeyframeCount = storyboardShots.filter((shot) =>
    storyboardShotKeyframeStale(
      shot,
      storyboardAssets,
      storyboardExternalReferenceImageIds,
    ),
  ).length;
  const completedVideoItems = useMemo(
    () => effectiveItems.filter(hasVideo),
    [effectiveItems],
  );
  const playbackVideoItem = useMemo(
    () =>
      selectedVideoId
        ? completedVideoItems.find((item) => item.video.id === selectedVideoId)
        : undefined,
    [completedVideoItems, selectedVideoId],
  );
  const settledHistoryItems = useMemo(
    () => effectiveItems.filter((item) => !isActiveVideo(item)),
    [effectiveItems],
  );
  const succeededHistoryItems = useMemo(
    () => settledHistoryItems.filter((item) => item.status === "succeeded"),
    [settledHistoryItems],
  );
  const failedHistoryItems = useMemo(
    () => settledHistoryItems.filter(isFailedHistoryVideo),
    [settledHistoryItems],
  );
  const filteredHistoryItems = useMemo(() => {
    if (historyFilter === "succeeded") return succeededHistoryItems;
    if (historyFilter === "failed") return failedHistoryItems;
    return settledHistoryItems;
  }, [failedHistoryItems, historyFilter, settledHistoryItems, succeededHistoryItems]);
  const channels = useMemo(
    () => activeItems.map((item) => `task:${item.id}`),
    [activeItems],
  );
  const activeItemIdsKey = useMemo(
    () => activeItems.map((item) => item.id).join("|"),
    [activeItems],
  );

  useEffect(() => {
    prewarmVideoItem(playbackVideoItem);
  }, [playbackVideoItem]);

  const refreshGeneration = useCallback(
    async (id: string, opts: { forceHistorySync?: boolean } = {}) => {
      const next = await getVideoGeneration(id);
      setItems((prev) => mergeById(prev, [next]));
      if (next.video) {
        prewarmVideoItem(next as VideoGenerationWithVideo);
      }

      const terminal = isTerminalVideo(next);
      if (!terminal) {
        terminalHistorySyncedRef.current.delete(id);
      }
      if (
        opts.forceHistorySync ||
        (terminal && !terminalHistorySyncedRef.current.has(id))
      ) {
        if (terminal) terminalHistorySyncedRef.current.add(id);
        await qc.invalidateQueries({ queryKey: ["video", "generations"] });
      }
    },
    [qc],
  );

  const refreshGenerationSafe = useCallback(
    async (id: string, opts: { forceHistorySync?: boolean } = {}) => {
      if (opts.forceHistorySync) {
        pendingHistoryRefreshRef.current.add(id);
      }
      if (refreshInFlightRef.current.has(id)) return;

      refreshInFlightRef.current.add(id);
      const forceHistorySync =
        opts.forceHistorySync || pendingHistoryRefreshRef.current.has(id);
      pendingHistoryRefreshRef.current.delete(id);

      try {
        await refreshGeneration(id, { forceHistorySync });
        refreshFailureCountRef.current.delete(id);
        refreshBackoffUntilRef.current.delete(id);
      } catch (err) {
        const nextFailures = (refreshFailureCountRef.current.get(id) ?? 0) + 1;
        refreshFailureCountRef.current.set(id, nextFailures);
        const backoffMs = Math.min(
          VIDEO_REFRESH_RETRY_MAX_MS,
          VIDEO_REFRESH_RETRY_BASE_MS * 2 ** Math.min(nextFailures - 1, 4),
        );
        refreshBackoffUntilRef.current.set(id, Date.now() + backoffMs);
        try {
          console.warn("[video] generation refresh failed", {
            id,
            failures: nextFailures,
            retryInMs: backoffMs,
            err,
          });
        } catch {
          /* console unavailable */
        }
      } finally {
        refreshInFlightRef.current.delete(id);
      }
    },
    [refreshGeneration],
  );

  const scheduleGenerationRefresh = useCallback(
    (
      id: string,
      opts: { forceHistorySync?: boolean; delayMs?: number } = {},
    ) => {
      if (!id) return;
      if (opts.forceHistorySync) {
        pendingHistoryRefreshRef.current.add(id);
      }
      if (scheduledRefreshTimersRef.current.has(id)) return;

      const now = Date.now();
      const lastRefreshAt = lastRefreshAtRef.current.get(id) ?? 0;
      const minIntervalDelay = Math.max(
        0,
        VIDEO_REFRESH_MIN_INTERVAL_MS - (now - lastRefreshAt),
      );
      const backoffDelay = Math.max(
        0,
        (refreshBackoffUntilRef.current.get(id) ?? 0) - now,
      );
      const delayMs = Math.max(opts.delayMs ?? 0, minIntervalDelay, backoffDelay);

      const timer = window.setTimeout(() => {
        scheduledRefreshTimersRef.current.delete(id);
        lastRefreshAtRef.current.set(id, Date.now());
        const forceHistorySync = pendingHistoryRefreshRef.current.has(id);
        pendingHistoryRefreshRef.current.delete(id);
        void refreshGenerationSafe(id, { forceHistorySync });
      }, delayMs);
      scheduledRefreshTimersRef.current.set(id, timer);
    },
    [refreshGenerationSafe],
  );

  const applyVideoEventSnapshot = useCallback(
    (data: unknown): { id: string; terminal: boolean } | null => {
      if (typeof data !== "object" || data === null) return null;
      const raw = data as {
        video_generation_id?: unknown;
        status?: unknown;
        stage?: unknown;
        progress_pct?: unknown;
        error_code?: unknown;
      };
      const id =
        typeof raw.video_generation_id === "string" ? raw.video_generation_id : "";
      if (!id) return null;

      const status = typeof raw.status === "string" ? raw.status : undefined;
      const stage = typeof raw.stage === "string" ? raw.stage : undefined;
      const progressPct =
        typeof raw.progress_pct === "number" ? raw.progress_pct : undefined;
      const errorCode =
        typeof raw.error_code === "string" ? raw.error_code : undefined;

      if (status || stage || progressPct !== undefined || errorCode) {
        setItems((prev) =>
          prev.map((item) =>
            item.id === id
              ? {
                  ...item,
                  ...(status
                    ? { status: status as VideoGenerationOut["status"] }
                    : {}),
                  ...(stage
                    ? {
                        progress_stage:
                          stage as VideoGenerationOut["progress_stage"],
                      }
                    : {}),
                  ...(progressPct !== undefined ? { progress_pct: progressPct } : {}),
                  ...(errorCode ? { error_code: errorCode } : {}),
                }
              : item,
          ),
        );
      }

      return { id, terminal: isTerminalVideoStatus(status) };
    },
    [],
  );

  const handlers = useMemo(
    () =>
      Object.fromEntries(
        VIDEO_EVENTS.map((eventName) => [
          eventName,
          (data: unknown) => {
            const snapshot = applyVideoEventSnapshot(data);
            if (snapshot) {
              scheduleGenerationRefresh(snapshot.id, {
                forceHistorySync: snapshot.terminal,
              });
            }
          },
        ]),
      ),
    [applyVideoEventSnapshot, scheduleGenerationRefresh],
  );
  useSSE(channels, handlers);

  useEffect(() => {
    const ids = activeItemIdsKey.split("|").filter(Boolean);
    if (ids.length === 0) return;

    let alive = true;
    const poll = () => {
      if (!alive) return;
      for (const id of ids) scheduleGenerationRefresh(id);
    };

    const initialTimer = window.setTimeout(poll, 800);
    const interval = window.setInterval(poll, VIDEO_ACTIVE_POLL_MS);

    return () => {
      alive = false;
      window.clearTimeout(initialTimer);
      window.clearInterval(interval);
    };
  }, [activeItemIdsKey, scheduleGenerationRefresh]);

  useEffect(() => {
    const refreshVisibleTasks = () => {
      if (document.visibilityState !== "visible") return;
      void qc.invalidateQueries({ queryKey: ["video", "generations"] });
      const ids = activeItemIdsKey.split("|").filter(Boolean);
      for (const id of ids) scheduleGenerationRefresh(id);
    };

    window.addEventListener("focus", refreshVisibleTasks);
    document.addEventListener("visibilitychange", refreshVisibleTasks);
    return () => {
      window.removeEventListener("focus", refreshVisibleTasks);
      document.removeEventListener("visibilitychange", refreshVisibleTasks);
    };
  }, [activeItemIdsKey, qc, scheduleGenerationRefresh]);

  useEffect(
    () => () => {
      for (const timer of scheduledRefreshTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      scheduledRefreshTimersRef.current.clear();
    },
    [],
  );

  const availableModels = useMemo(
    () => options?.models.filter((item) => item.actions.includes(action)) ?? [],
    [action, options?.models],
  );
  const selectedModel = model || firstModelForAction(options, action);
  const selectedBillingModel = billingModelForAction(options, selectedModel, action);
  const availableResolutions = useMemo(
    () => resolutionOptionsForModel(options, selectedModel),
    [options, selectedModel],
  );
  const effectiveResolution = availableResolutions.includes(resolution)
    ? resolution
    : preferredResolution(availableResolutions);
  const estimate = estimateHoldMicro(options, {
    model: selectedModel,
    billingModel: selectedBillingModel,
    action,
    resolution: effectiveResolution,
    durationS,
    referenceHasVideo: referenceMedia.some((item) => item.kind === "video"),
  });
  const storyboardAction: VideoAction = "i2v";
  const storyboardModelSupportsAction = Boolean(
    model &&
      options?.models.some(
        (item) => item.model === model && item.actions.includes(storyboardAction),
      ),
  );
  const storyboardSelectedModel = storyboardModelSupportsAction
    ? model
    : firstModelForAction(options, storyboardAction);
  const storyboardBillingModel = billingModelForAction(
    options,
    storyboardSelectedModel,
    storyboardAction,
  );
  const storyboardResolutionOptions = resolutionOptionsForModel(
    options,
    storyboardSelectedModel,
  );
  const storyboardEffectiveResolution = storyboardResolutionOptions.includes(
    effectiveResolution,
  )
    ? effectiveResolution
    : preferredResolution(storyboardResolutionOptions);
  const storyboardEstimate = estimateHoldMicro(options, {
    model: storyboardSelectedModel,
    billingModel: storyboardBillingModel,
    action: storyboardAction,
    resolution: storyboardEffectiveResolution,
    durationS: selectedShot?.durationS ?? 5,
    referenceHasVideo: false,
  });
  const nextReferenceLabel = useCallback(
    (kind: "image" | "video") => {
      const count = referenceMedia.filter((item) => item.kind === kind).length + 1;
      return `${kind === "image" ? "图片" : "视频"} ${count}`;
    },
    [referenceMedia],
  );
  const clearPromptEnhanceChoices = useCallback(() => {
    setPromptEnhancePreview("");
    setPromptEnhanceCandidates([]);
    setSelectedPromptEnhanceCandidateId("");
  }, []);

  const insertPromptText = useCallback((text: string) => {
    clearPromptEnhanceChoices();
    const target = promptRef.current;
    if (!target) {
      setPrompt((prev) => `${prev}${prev.endsWith(" ") || !prev ? "" : " "}${text}`);
      return;
    }
    const start = target.selectionStart ?? prompt.length;
    const end = target.selectionEnd ?? prompt.length;
    const before = prompt.slice(0, start);
    const after = prompt.slice(end);
    const spacer = before && !before.endsWith(" ") ? " " : "";
    const next = `${before}${spacer}${text}${after.startsWith(" ") || !after ? "" : " "}${after}`;
    setPrompt(next);
    requestAnimationFrame(() => {
      const pos = (before + spacer + text).length;
      target.focus();
      target.setSelectionRange(pos, pos);
    });
  }, [clearPromptEnhanceChoices, prompt]);

  const insertReferenceTag = useCallback((label: string) => {
    insertPromptText(`[${label}]`);
  }, [insertPromptText]);

  const uploadMut = useMutation({
    mutationFn: (file: File) => uploadImage(file),
    onSuccess: (img) => {
      clearPromptEnhanceChoices();
      setInputImageId(img.id);
      setUploadedLabel(`${img.width}x${img.height}`);
      toast.success("首帧已上传");
    },
    onError: (err) => toast.error("上传失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const referenceUploadMut = useMutation({
    mutationFn: async (file: File) => {
      if (workspaceMode === "storyboard" && !file.type.startsWith("image/")) {
        throw new Error("故事板参考只支持图片，视频参考请在单条参考生成中使用");
      }
      if (file.type.startsWith("image/")) {
        if (referenceMedia.filter((item) => item.kind === "image").length >= 9) {
          throw new Error("参考图片最多 9 张");
        }
        const img = await uploadImage(file);
        return {
          kind: "image" as const,
          image_id: img.id,
          display: `${img.width}x${img.height}`,
        };
      }
      if (file.type.startsWith("video/")) {
        if (referenceMedia.filter((item) => item.kind === "video").length >= 3) {
          throw new Error("参考视频最多 3 个");
        }
        const video = await uploadVideo(file);
        return {
          kind: "video" as const,
          video_id: video.id,
          display: video.size_bytes ? `${Math.round(video.size_bytes / 1024 / 1024)}MB` : "视频",
        };
      }
      throw new Error("只支持图片或视频");
    },
    onSuccess: (ref) => {
      clearPromptEnhanceChoices();
      const label = nextReferenceLabel(ref.kind);
      setReferenceMedia((prev) => [
        ...prev,
        {
          _key: uuid(),
          kind: ref.kind,
          image_id: ref.kind === "image" ? ref.image_id : null,
          video_id: ref.kind === "video" ? ref.video_id : null,
          label,
          display: ref.display,
        },
      ]);
      toast.success("参考素材已上传");
    },
    onError: (err) => toast.error("上传失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const addAssetReference = useCallback(() => {
    const url = normalizeAssetUrl(assetUrlInput);
    if (!url) return;
    if (referenceMedia.filter((item) => item.kind === "image").length >= 9) {
      toast.error("参考图片最多 9 张");
      return;
    }
    clearPromptEnhanceChoices();
    const label = nextReferenceLabel("image");
    setReferenceMedia((prev) => [
      ...prev,
      {
        _key: uuid(),
        kind: "image",
        url,
        label,
        display: url,
      },
    ]);
    setAssetUrlInput("");
    toast.success("官方素材已添加");
  }, [
    assetUrlInput,
    clearPromptEnhanceChoices,
    nextReferenceLabel,
    referenceMedia,
  ]);

  const ensureStoryboardConversation = useCallback(async () => {
    if (storyboardConversationId) return storyboardConversationId;
    const conversation = await createConversation({
      title: `分镜预生产 · ${storyboardTitle || "未命名项目"}`,
      default_params: {
        source: "video_storyboard_workspace",
      },
    });
    setStoryboardConversationId(conversation.id);
    return conversation.id;
  }, [storyboardConversationId, storyboardTitle]);

  const runStoryboardTextTask = useCallback(
    async (text: string) => {
      const conversationId = await ensureStoryboardConversation();
      const out = await postMessage(conversationId, {
        idempotency_key: uuid(),
        text,
        intent: "chat",
        source: "video_storyboard_workspace",
        action_source: "storyboard.preproduction.chat",
        trace_id: uuid(),
        chat_params: { fast: false },
      });
      const completionId = out.completion_id;
      if (!completionId) throw new Error("AI 未返回文本任务");
      const task = await waitForStoryboardCompletion(completionId);
      if (task.status !== "succeeded") {
        throw new Error(task.error_message || "AI 文本任务失败");
      }
      return task.text.trim();
    },
    [ensureStoryboardConversation],
  );

  const handleStoryboardScriptChange = useCallback((value: string) => {
    setStoryboardScript(value);
    setScriptConfirmed(false);
    setScriptRevision((prev) => prev + 1);
    setStoryboardStage("script");
  }, []);

  const expandStoryboardIdea = useCallback(async () => {
    if (isExpandingScript) return;
    setIsExpandingScript(true);
    try {
      const nextScript = await runStoryboardTextTask(
        formatScriptExpansionPrompt({
          idea: storyboardIdea,
          title: storyboardTitle,
          style: storyboardStyle,
        }),
      );
      setStoryboardScript(nextScript);
      setScriptConfirmed(false);
      setScriptRevision((prev) => prev + 1);
      setStoryboardStage("script");
      toast.success("脚本已扩写，请确认后进入资产设定");
    } catch (err) {
      toast.error("脚本扩写失败", {
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setIsExpandingScript(false);
    }
  }, [
    isExpandingScript,
    runStoryboardTextTask,
    storyboardIdea,
    storyboardStyle,
    storyboardTitle,
  ]);

  const confirmStoryboardScript = useCallback(() => {
    if (!storyboardScript.trim()) {
      toast.error("先填写或扩写脚本");
      return;
    }
    setScriptConfirmed(true);
    setScriptApprovedRevision(scriptRevision);
    setScriptApprovedAt(nowIso());
    setStoryboardStage("assets");
    toast.success(`脚本 v${scriptRevision} 已锁定`);
  }, [scriptRevision, storyboardScript]);

  const extractStoryboardAssets = useCallback(async () => {
    if (isExtractingAssets) return;
    if (!storyboardScript.trim()) {
      toast.error("先确认脚本");
      return;
    }
    setIsExtractingAssets(true);
    try {
      const text = await runStoryboardTextTask(
        formatAssetExtractionPrompt(storyboardScript),
      );
      const assets = parseAssetsFromAiText(text);
      if (assets.length === 0) throw new Error("AI 未返回可用的人物/场景/道具 JSON");
      setStoryboardAssets(assets);
      setStoryboardShots((prev) =>
        prev.map((shot) => ({
          ...shot,
          assetIds: bindAllStoryboardAssetIds(assets),
          keyframeApproved: false,
          videoApproved: false,
        })),
      );
      setStoryboardStage("assets");
      toast.success(`已提取 ${assets.length} 个一致性资产`);
    } catch (err) {
      toast.error("提取人物/场景失败", {
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setIsExtractingAssets(false);
    }
  }, [isExtractingAssets, runStoryboardTextTask, storyboardScript]);

  const addStoryboardAsset = useCallback((kind: StoryboardAssetKind) => {
    const next = createStoryboardAsset(kind, {
      name:
        kind === "character"
          ? "新增人物"
          : kind === "scene"
            ? "新增场景"
            : "新增道具",
    });
    setStoryboardAssets((prev) => [...prev, next]);
    setStoryboardStage("assets");
  }, []);

  const updateStoryboardAsset = useCallback(
    (assetId: string, patch: StoryboardReferenceAssetPatch) => {
      setStoryboardAssets((prev) =>
        prev.map((asset) =>
          asset.id === assetId
            ? {
                ...asset,
                ...patch,
                revision: asset.revision + 1,
                approved: false,
                approvedAt: undefined,
              }
            : asset,
        ),
      );
    },
    [],
  );

  const approveStoryboardAsset = useCallback((assetId: string) => {
    setStoryboardAssets((prev) =>
      prev.map((asset) =>
        asset.id === assetId
          ? {
              ...asset,
              approved: Boolean(asset.imageId),
              approvedAt: asset.imageId ? nowIso() : asset.approvedAt,
            }
          : asset,
      ),
    );
  }, []);

  const revokeStoryboardAssetApproval = useCallback((assetId: string) => {
    setStoryboardAssets((prev) =>
      prev.map((asset) =>
        asset.id === assetId
          ? {
              ...asset,
              approved: false,
              approvedAt: undefined,
              revision: asset.revision + 1,
            }
          : asset,
      ),
    );
  }, []);

  const removeStoryboardAsset = useCallback((assetId: string) => {
    setStoryboardAssets((prev) => {
      if (prev.length <= 1) {
        toast.error("至少保留一个人物、场景或道具参考");
        return prev;
      }
      return prev.filter((asset) => asset.id !== assetId);
    });
  }, []);

  const submitStoryboardImageTask = useCallback(
    async ({
      promptText,
      inputImageIds,
      actionSource,
    }: {
      promptText: string;
      inputImageIds: string[];
      actionSource: string;
    }) => {
      const conversationId = await ensureStoryboardConversation();
      const hasInputs = inputImageIds.length > 0;
      const out = await postMessage(conversationId, {
        idempotency_key: uuid(),
        text: promptText,
        attachment_image_ids: inputImageIds,
        input_images: inputImageIds,
        attachments: inputImageIds.map((imageId) => ({
          image_id: imageId,
          role: "reference",
          weight: 0.85,
        })),
        intent: hasInputs ? "image_to_image" : "text_to_image",
        source: "video_storyboard_workspace",
        action_source: actionSource,
        trace_id: uuid(),
        image_params: {
          aspect_ratio: storyboardImageAspect(aspectRatio),
          size_mode: "auto",
          quality: "1k",
          count: 1,
          fast: false,
          render_quality: "medium",
          background: "auto",
          moderation: "low",
        },
      });
      const generationId = out.generation_ids?.[0];
      if (!generationId) throw new Error("AI 未返回图片任务");
      const initialTask = await getTask("generations", generationId);
      setStoryboardImageTasks((prev) => mergeImageTasks(prev, [initialTask]));
      const finalTask = await waitForStoryboardImageTask(generationId);
      setStoryboardImageTasks((prev) => mergeImageTasks(prev, [finalTask]));
      if (finalTask.status !== "succeeded") {
        throw new Error(finalTask.error_message || "图片任务失败");
      }
      const image = await findGeneratedImage(conversationId, generationId);
      if (!image) throw new Error("图片生成成功，但没有找到输出图");
      return { task: finalTask, image };
    },
    [aspectRatio, ensureStoryboardConversation],
  );

  const generateStoryboardAssetImage = useCallback(
    async (assetId: string) => {
      const asset = storyboardAssets.find((item) => item.id === assetId);
      if (!asset || generatingAssetId) return;
      setGeneratingAssetId(assetId);
      try {
        const promptText =
          asset.prompt ||
          formatStoryboardAssetPrompt({
            asset,
            script: storyboardScript,
            style: storyboardStyle,
          });
        const { task, image } = await submitStoryboardImageTask({
          promptText,
          inputImageIds: [],
          actionSource: `storyboard.${asset.kind}.reference_image`,
        });
        setStoryboardAssets((prev) =>
          prev.map((item) =>
            item.id === assetId
              ? {
                  ...item,
                  prompt: promptText,
                  revision: item.revision + 1,
                  approved: false,
                  approvedAt: undefined,
                  generationId: task.id,
                  imageId: image.id,
                  imageUrl: imagePreviewUrl(image),
                }
              : item,
          ),
        );
        setStoryboardStage("assets");
        toast.success(`${storyboardAssetLabel(asset.kind)}图已生成，请批准后用于分镜`);
      } catch (err) {
        toast.error("参考图生成失败", {
          description: err instanceof Error ? err.message : undefined,
        });
      } finally {
        setGeneratingAssetId("");
      }
    },
    [
      generatingAssetId,
      storyboardAssets,
      storyboardScript,
      storyboardStyle,
      submitStoryboardImageTask,
    ],
  );

  const updateStoryboardShot = useCallback(
    (shotId: string, patch: StoryboardShotPatch) => {
      const invalidatesApproval =
        patch.title != null ||
        patch.purpose != null ||
        patch.durationS != null ||
        patch.narration != null ||
        patch.visual != null ||
        patch.shotType != null ||
        patch.cameraMove != null ||
        patch.transition != null ||
        patch.referenceNotes != null ||
        patch.keyframePrompt != null ||
        patch.assetIds != null;
      setStoryboardShots((prev) =>
        prev.map((shot) =>
          shot.id === shotId
            ? {
                ...shot,
                ...patch,
                approved: invalidatesApproval ? false : (patch.approved ?? shot.approved),
                approvedAt: invalidatesApproval ? undefined : (patch.approvedAt ?? shot.approvedAt),
                keyframeApproved: invalidatesApproval
                  ? false
                  : (patch.keyframeApproved ?? shot.keyframeApproved),
                keyframeApprovedAt: invalidatesApproval
                  ? undefined
                  : (patch.keyframeApprovedAt ?? shot.keyframeApprovedAt),
                videoApproved: invalidatesApproval
                  ? false
                  : (patch.videoApproved ?? shot.videoApproved),
                videoApprovedAt: invalidatesApproval
                  ? undefined
                  : (patch.videoApprovedAt ?? shot.videoApprovedAt),
                durationS:
                  patch.durationS != null
                    ? clampStoryboardDuration(patch.durationS)
                    : shot.durationS,
              }
            : shot,
        ),
      );
    },
    [],
  );

  const approveStoryboardShot = useCallback((shotId: string) => {
    setStoryboardShots((prev) =>
      prev.map((shot) =>
        shot.id === shotId
          ? {
              ...shot,
              approved: true,
              approvedAt: nowIso(),
            }
          : shot,
      ),
    );
  }, []);

  const approveStoryboardShotKeyframe = useCallback((shotId: string) => {
    setStoryboardShots((prev) =>
      prev.map((shot) => {
        if (shot.id !== shotId || !shot.keyframeImageId) return shot;
        return {
          ...shot,
          keyframeApproved: true,
          keyframeApprovedAt: nowIso(),
          keyframeSourceHash: storyboardShotSourceHash(
            shot,
            storyboardAssets,
            storyboardExternalReferenceImageIds,
          ),
        };
      }),
    );
  }, [storyboardAssets, storyboardExternalReferenceImageIds]);

  const selectedShotKeyframePrompt = useMemo(() => {
    if (!selectedShot) return "";
    if (selectedShot.keyframePrompt.trim()) return selectedShot.keyframePrompt;
    const shotNumber = storyboardShots.findIndex((shot) => shot.id === selectedShot.id) + 1;
    return formatStoryboardKeyframePrompt({
      projectTitle: storyboardTitle,
      projectStyle: storyboardStyle,
      shot: selectedShot,
      shotNumber: Math.max(1, shotNumber),
      totalShots: storyboardShots.length,
      hasReferences:
        approvedImageIdsForShot(selectedShot, storyboardAssets).length > 0 ||
        storyboardExternalReferenceImageIds.length > 0,
    });
  }, [
    selectedShot,
    storyboardAssets,
    storyboardExternalReferenceImageIds,
    storyboardShots,
    storyboardStyle,
    storyboardTitle,
  ]);

  const generateStoryboardShotKeyframe = useCallback(
    async (shotId: string) => {
      const shot = storyboardShots.find((item) => item.id === shotId);
      if (!shot || generatingKeyframeShotId) return;
      if (!shot.approved) {
        toast.error("先批准镜头", {
          description: "镜头内容确认后再生成分镜图，避免反复浪费图片任务。",
        });
        return;
      }
      const shotAssetImageIds = approvedImageIdsForShot(shot, storyboardAssets);
      const inputImageIds = mergeStoryboardReferenceImageIds(
        shotAssetImageIds,
        storyboardExternalReferenceImageIds,
      );
      if (storyboardAssets.length > 0 && inputImageIds.length === 0) {
        toast.error("先批准设定图或上传参考图片", {
          description: "人物、场景、道具图或侧栏参考图片会作为分镜图输入。",
        });
        return;
      }
      setGeneratingKeyframeShotId(shotId);
      try {
        const shotNumber = storyboardShots.findIndex((item) => item.id === shotId) + 1;
        const promptText =
          shot.keyframePrompt ||
          formatStoryboardKeyframePrompt({
            projectTitle: storyboardTitle,
            projectStyle: storyboardStyle,
            shot,
            shotNumber: Math.max(1, shotNumber),
            totalShots: storyboardShots.length,
            hasReferences: inputImageIds.length > 0,
          });
        const { task, image } = await submitStoryboardImageTask({
          promptText,
          inputImageIds,
          actionSource: "storyboard.shot.keyframe",
        });
        setStoryboardShots((prev) =>
          prev.map((item) =>
            item.id === shotId
              ? {
                  ...item,
                  keyframePrompt: promptText,
                  keyframeGenerationId: task.id,
                  keyframeImageId: image.id,
                  keyframeImageUrl: imagePreviewUrl(image),
                  keyframeApproved: false,
                  keyframeApprovedAt: undefined,
                  keyframeSourceHash: storyboardShotSourceHash(
                    item,
                    storyboardAssets,
                    storyboardExternalReferenceImageIds,
                  ),
                  videoApproved: false,
                  videoApprovedAt: undefined,
                }
              : item,
          ),
        );
        setStoryboardStage("keyframes");
        toast.success("分镜图已生成");
      } catch (err) {
        toast.error("分镜图生成失败", {
          description: err instanceof Error ? err.message : undefined,
        });
      } finally {
        setGeneratingKeyframeShotId("");
      }
    },
    [
      generatingKeyframeShotId,
      storyboardAssets,
      storyboardExternalReferenceImageIds,
      storyboardShots,
      storyboardStyle,
      storyboardTitle,
      submitStoryboardImageTask,
    ],
  );

  const generateAllStoryboardKeyframes = useCallback(async () => {
    if (generatingKeyframeShotId) return;
    const pending = storyboardShots.filter((shot) => !shot.keyframeImageId);
    if (pending.length === 0) {
      toast.success("所有分镜图都已生成");
      return;
    }
    for (const shot of pending) {
      await generateStoryboardShotKeyframe(shot.id);
    }
  }, [generateStoryboardShotKeyframe, generatingKeyframeShotId, storyboardShots]);

  const rebuildStoryboardFromScript = useCallback(() => {
    if (!scriptConfirmed) {
      setStoryboardStage("script");
      toast.error("先确认脚本", {
        description: "脚本确认后再拆分镜，避免后续资产和镜头反复失配。",
      });
      return;
    }
    const next = createShotsFromScript(storyboardScript).map((shot) => ({
      ...shot,
      assetIds: bindAllStoryboardAssetIds(storyboardAssets),
    }));
    if (next.length === 0) {
      toast.error("先粘贴脚本", { description: "至少需要一段文案或一行镜头描述。" });
      return;
    }
    setStoryboardShots(next);
    setSelectedShotId(next[0]?.id ?? "");
    setStoryboardStage("shots");
    toast.success(`已拆成 ${next.length} 个镜头`);
  }, [scriptConfirmed, storyboardAssets, storyboardScript]);

  const addStoryboardShot = useCallback(() => {
    const next = createStoryboardShot({
      title: `镜头 ${storyboardShots.length + 1}`,
      assetIds: bindAllStoryboardAssetIds(storyboardAssets),
    });
    setStoryboardShots((prev) => [...prev, next]);
    setSelectedShotId(next.id);
  }, [storyboardAssets, storyboardShots.length]);

  const duplicateStoryboardShot = useCallback((shotId: string) => {
    setStoryboardShots((prev) => {
      const index = prev.findIndex((shot) => shot.id === shotId);
      if (index < 0) return prev;
      const source = prev[index];
      const copy = createStoryboardShot({
        ...source,
        id: undefined,
        generationId: undefined,
        keyframeGenerationId: undefined,
        keyframeImageId: undefined,
        keyframeImageUrl: undefined,
        keyframeApproved: false,
        keyframeApprovedAt: undefined,
        keyframeSourceHash: undefined,
        videoApproved: false,
        videoApprovedAt: undefined,
        approved: false,
        approvedAt: undefined,
        title: `${source.title} 副本`,
      });
      const next = [...prev.slice(0, index + 1), copy, ...prev.slice(index + 1)];
      setSelectedShotId(copy.id);
      return next;
    });
  }, []);

  const removeStoryboardShot = useCallback((shotId: string) => {
    setStoryboardShots((prev) => {
      if (prev.length <= 1) {
        toast.error("至少保留一个镜头");
        return prev;
      }
      const index = prev.findIndex((shot) => shot.id === shotId);
      const next = prev.filter((shot) => shot.id !== shotId);
      if (shotId === selectedShotId) {
        setSelectedShotId(next[Math.max(0, index - 1)]?.id ?? next[0]?.id ?? "");
      }
      return next;
    });
  }, [selectedShotId]);

  const moveStoryboardShot = useCallback((shotId: string, direction: -1 | 1) => {
    setStoryboardShots((prev) => {
      const index = prev.findIndex((shot) => shot.id === shotId);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= prev.length) return prev;
      const next = [...prev];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  }, []);

  const selectedShotPrompt = useMemo(() => {
    if (!selectedShot) return "";
    const shotNumber = storyboardShots.findIndex((shot) => shot.id === selectedShot.id) + 1;
    return formatStoryboardShotPrompt({
      projectTitle: storyboardTitle,
      projectStyle: storyboardStyle,
      shot: selectedShot,
      shotNumber: Math.max(1, shotNumber),
      totalShots: storyboardShots.length,
      hasReferences:
        approvedImageIdsForShot(selectedShot, storyboardAssets).length > 0 ||
        storyboardExternalReferenceImageIds.length > 0 ||
        Boolean(selectedShot.keyframeImageId),
    });
  }, [
    selectedShot,
    storyboardAssets,
    storyboardExternalReferenceImageIds,
    storyboardShots,
    storyboardStyle,
    storyboardTitle,
  ]);

  const storyboardSubmitDisabledReason = useMemo(() => {
    if (isSubmittingStoryboard) return "正在提交分镜";
    if (optionsQ.isLoading) return "正在读取配置";
    if (!options?.enabled) return options?.unavailable_reason ?? "功能未启用";
    if (!storyboardSelectedModel) return "没有可用模型";
    if (!storyboardResolutionOptions.includes(storyboardEffectiveResolution)) {
      return "当前模型不支持该分辨率";
    }
    if (!selectedShot) return "先添加镜头";
    if (!selectedShot.keyframeImageId) return "先生成并确认分镜图";
    if (
      storyboardShotKeyframeStale(
        selectedShot,
        storyboardAssets,
        storyboardExternalReferenceImageIds,
      )
    ) return "分镜图已过期";
    if (!selectedShot.keyframeApproved) return "先批准分镜图";
    if (storyboardEstimate === null) return "缺少预扣估算";
    return "可用分镜图生成视频";
  }, [
    isSubmittingStoryboard,
    options?.enabled,
    options?.unavailable_reason,
    optionsQ.isLoading,
    selectedShot,
    storyboardAssets,
    storyboardExternalReferenceImageIds,
    storyboardEffectiveResolution,
    storyboardEstimate,
    storyboardResolutionOptions,
    storyboardSelectedModel,
  ]);

  const canSubmitStoryboardShot =
    Boolean(options?.enabled) &&
    Boolean(storyboardSelectedModel) &&
    Boolean(selectedShot?.keyframeImageId) &&
    Boolean(selectedShot?.keyframeApproved) &&
    Boolean(
      selectedShot &&
        !storyboardShotKeyframeStale(
          selectedShot,
          storyboardAssets,
          storyboardExternalReferenceImageIds,
        ),
    ) &&
    storyboardResolutionOptions.includes(storyboardEffectiveResolution) &&
    storyboardEstimate !== null &&
    !isSubmittingStoryboard;

  const submitStoryboardShot = useCallback(
    async (shotId: string, opts: { quiet?: boolean } = {}) => {
      const shot = storyboardShots.find((item) => item.id === shotId);
      if (!shot) throw new Error("镜头不存在");
      if (!shot.keyframeImageId) throw new Error("先生成这个镜头的分镜图");
      if (
        storyboardShotKeyframeStale(
          shot,
          storyboardAssets,
          storyboardExternalReferenceImageIds,
        )
      ) {
        throw new Error("这个镜头的分镜图已过期，请重新生成或重新批准");
      }
      if (!shot.keyframeApproved) throw new Error("先批准这个镜头的分镜图");
      if (!options?.enabled) {
        throw new Error(options?.unavailable_reason ?? "视频功能未启用");
      }
      if (!storyboardSelectedModel) throw new Error("没有可用模型");
      const shotEstimate = estimateHoldMicro(options, {
        model: storyboardSelectedModel,
        billingModel: storyboardBillingModel,
        action: storyboardAction,
        resolution: storyboardEffectiveResolution,
        durationS: shot.durationS,
        referenceHasVideo: false,
      });
      if (shotEstimate === null) throw new Error("缺少预扣估算");
      const shotNumber = storyboardShots.findIndex((item) => item.id === shotId) + 1;
      const gen = await createVideoGeneration({
        action: storyboardAction,
        model: storyboardSelectedModel,
        prompt: formatStoryboardShotPrompt({
          projectTitle: storyboardTitle,
          projectStyle: storyboardStyle,
          shot,
          shotNumber: Math.max(1, shotNumber),
          totalShots: storyboardShots.length,
          hasReferences: true,
        }),
        input_image_id: shot.keyframeImageId,
        reference_media: [],
        duration_s: clampStoryboardDuration(shot.durationS),
        resolution: toVideoResolution(storyboardEffectiveResolution),
        aspect_ratio: aspectRatio,
        generate_audio: generateAudio,
        seed: parseSeed(seed),
        watermark: false,
      });
      terminalHistorySyncedRef.current.delete(gen.id);
      setItems((prev) => mergeById(prev, [gen]));
      setStoryboardShots((prev) =>
        prev.map((item) =>
          item.id === shotId ? { ...item, generationId: gen.id } : item,
        ),
      );
      scheduleGenerationRefresh(gen.id, { delayMs: 800 });
      void qc.invalidateQueries({ queryKey: ["video", "generations"] });
      if (!opts.quiet) toast.success("镜头任务已提交");
      return gen;
    },
    [
      aspectRatio,
      generateAudio,
      options,
      qc,
      scheduleGenerationRefresh,
      seed,
      storyboardAction,
      storyboardBillingModel,
      storyboardEffectiveResolution,
      storyboardExternalReferenceImageIds,
      storyboardSelectedModel,
      storyboardAssets,
      storyboardShots,
      storyboardStyle,
      storyboardTitle,
    ],
  );

  const submitSelectedStoryboardShot = useCallback(async () => {
    if (!selectedShot || isSubmittingStoryboard) return;
    setIsSubmittingStoryboard(true);
    try {
      await submitStoryboardShot(selectedShot.id);
    } catch (err) {
      toast.error("提交失败", { description: err instanceof Error ? err.message : undefined });
    } finally {
      setIsSubmittingStoryboard(false);
    }
  }, [isSubmittingStoryboard, selectedShot, submitStoryboardShot]);

  const submitStoryboardQueue = useCallback(async () => {
    if (isSubmittingStoryboard || storyboardShots.length === 0) return;
    setIsSubmittingStoryboard(true);
    let submitted = 0;
    try {
      for (const shot of storyboardShots) {
        await submitStoryboardShot(shot.id, { quiet: true });
        submitted += 1;
      }
      toast.success(`已提交 ${submitted} 个镜头`);
    } catch (err) {
      toast.error("批量提交中断", {
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setIsSubmittingStoryboard(false);
    }
  }, [isSubmittingStoryboard, storyboardShots, submitStoryboardShot]);

  const applySelectedShotToSingleGenerator = useCallback(() => {
    if (!selectedShot) return;
    clearPromptEnhanceChoices();
    setAction(storyboardAction);
    setPrompt(selectedShotPrompt);
    setInputImageId(selectedShot.keyframeImageId ?? "");
    setUploadedLabel(
      selectedShot.keyframeImageId ? `分镜图 · ${selectedShot.title}` : "",
    );
    setDurationS(clampStoryboardDuration(selectedShot.durationS));
    setResolution(storyboardEffectiveResolution);
    setWorkspaceMode("single");
    requestAnimationFrame(() => promptRef.current?.focus());
  }, [
    clearPromptEnhanceChoices,
    selectedShot,
    selectedShotPrompt,
    storyboardAction,
    storyboardEffectiveResolution,
  ]);

  const createMut = useMutation({
    mutationFn: () =>
      createVideoGeneration({
        action,
        model: selectedModel,
        prompt: prompt.trim(),
        input_image_id: action === "i2v" ? inputImageId.trim() : null,
        reference_media:
          action === "reference"
            ? referenceMedia.map(referenceMediaPayload)
            : [],
        duration_s: durationS,
        resolution: toVideoResolution(effectiveResolution),
        aspect_ratio: aspectRatio,
        generate_audio: generateAudio,
        seed: parseSeed(seed),
        watermark: false,
      }),
    onSuccess: (gen) => {
      terminalHistorySyncedRef.current.delete(gen.id);
      setItems((prev) => mergeById(prev, [gen]));
      toast.success("任务已提交");
      scheduleGenerationRefresh(gen.id, { delayMs: 800 });
      void qc.invalidateQueries({ queryKey: ["video", "generations"] });
    },
    onError: (err) => toast.error("提交失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const cancelMut = useMutation({
    mutationFn: cancelVideoGeneration,
    onSuccess: (gen) => {
      setItems((prev) => mergeById(prev, [gen]));
      toast.success("已请求取消");
      scheduleGenerationRefresh(gen.id, { forceHistorySync: true });
    },
    onError: (err) => toast.error("取消失败", { description: err instanceof Error ? err.message : undefined }),
  });
  const retryMut = useMutation({
    mutationFn: retryVideoGeneration,
    onSuccess: (gen) => {
      terminalHistorySyncedRef.current.delete(gen.id);
      setItems((prev) => mergeById(prev, [gen]));
      toast.success("已重新生成");
      scheduleGenerationRefresh(gen.id, { delayMs: 800 });
    },
    onError: (err) => toast.error("重试失败", { description: err instanceof Error ? err.message : undefined }),
  });
  const deleteMut = useMutation({
    mutationFn: deleteVideo,
    onSuccess: async (_data, videoId) => {
      setItems((prev) =>
        prev.map((item) =>
          item.video?.id === videoId ? { ...item, video: null } : item,
        ),
      );
      setSelectedVideoId((current) => (current === videoId ? "" : current));
      toast.success("视频已删除");
      await qc.invalidateQueries({ queryKey: ["video", "generations"] });
    },
    onError: (err) => toast.error("删除失败", { description: err instanceof Error ? err.message : undefined }),
  });

  const loadAsDraft = useCallback((item: VideoGenerationOut) => {
    clearPromptEnhanceChoices();
    setAction(item.action);
    setPrompt(item.prompt);
    setModel(item.model);
    setDurationS(item.duration_s);
    setResolution(item.resolution);
    setAspectRatio(item.aspect_ratio);
    setGenerateAudio(item.generate_audio);
    setSeed(item.seed != null ? String(item.seed) : "");
    setInputImageId(item.input_image_id ?? "");
    setUploadedLabel(item.input_image_id ? "已从历史任务载入" : "");
    setReferenceMedia(
      item.reference_media.map((ref, index) => {
        const kindIndex =
          item.reference_media
            .slice(0, index + 1)
            .filter((current) => current.kind === ref.kind).length;
        const fallbackLabel = `${ref.kind === "image" ? "图片" : "视频"} ${kindIndex}`;
        const label = ref.label || fallbackLabel;
        return {
          _key: uuid(),
          kind: ref.kind,
          image_id: ref.kind === "image" ? ref.image_id ?? null : null,
          video_id: ref.kind === "video" ? ref.video_id ?? null : null,
          url: ref.url ?? null,
          label,
          display:
            ref.url
              ? ref.url.replace(/^asset:\/\//i, "asset://")
              : ref.kind === "image"
              ? ref.image_id?.slice(0, 8) ?? "图片"
              : ref.video_id?.slice(0, 8) ?? "视频",
        };
      }),
    );
    requestAnimationFrame(() => promptRef.current?.focus());
    toast.success("已套用参数");
  }, [clearPromptEnhanceChoices]);

  const canEnhancePrompt = Boolean(
    prompt.trim() ||
      (action === "i2v" && inputImageId.trim()) ||
      (action === "reference" && referenceMedia.length > 0),
  );

  const enhancePromptAction = useCallback(async () => {
    if (isEnhancingPrompt || !canEnhancePrompt) return;
    const original = prompt;
    const current = prompt.trim();
    const ctl = new AbortController();
    promptEnhanceAbortRef.current?.abort();
    promptEnhanceAbortRef.current = ctl;
    clearPromptEnhanceChoices();
    setIsEnhancingPrompt(true);
    let accumulated = "";
    try {
      await enhanceVideoPrompt(
        {
          text: current,
          action,
          model: selectedModel,
          duration_s: durationS,
          resolution: effectiveResolution,
          aspect_ratio: aspectRatio,
          generate_audio: generateAudio,
          input_image_id: action === "i2v" ? inputImageId.trim() || null : null,
          variant_count: VIDEO_PROMPT_VARIANT_COUNT,
          reference_media:
            action === "reference"
              ? referenceMedia.map(referenceMediaPayload)
              : [],
        },
        (delta) => {
          if (ctl.signal.aborted || promptEnhanceAbortRef.current !== ctl) return;
          accumulated += delta;
          setPromptEnhancePreview(accumulated);
        },
        ctl.signal,
      );
      const candidates = parsePromptEnhanceCandidates(accumulated);
      const recommended = candidates[0];
      if (recommended) {
        setPrompt(recommended.prompt);
        setPromptEnhanceCandidates(candidates);
        setSelectedPromptEnhanceCandidateId(recommended.id);
        setPromptEnhancePreview("");
        toast.success(
          candidates.length > 1
            ? `已生成 ${candidates.length} 个优化方案`
            : "提示词已优化",
        );
      } else {
        setPromptEnhancePreview("");
        toast.error("优化失败", { description: "没有收到有效提示词" });
        setPrompt(original);
      }
    } catch (err) {
      if (!ctl.signal.aborted) {
        const description = err instanceof Error ? err.message : undefined;
        if (accumulated.trim()) {
          const candidates = parsePromptEnhanceCandidates(accumulated);
          const recommended = candidates[0];
          if (recommended) {
            setPrompt(recommended.prompt);
            setPromptEnhanceCandidates(candidates);
            setSelectedPromptEnhanceCandidateId(recommended.id);
          } else {
            setPrompt(cleanPromptEnhanceText(accumulated));
          }
          setPromptEnhancePreview("");
          toast.error("优化中断", {
            description: description
              ? `${description} 已保留已生成内容，可继续编辑或重试。`
              : "已保留已生成内容，可继续编辑或重试。",
          });
        } else {
          toast.error("优化失败", { description });
          setPrompt(original);
        }
      }
    } finally {
      if (promptEnhanceAbortRef.current === ctl) {
        promptEnhanceAbortRef.current = null;
      }
      setIsEnhancingPrompt(false);
    }
  }, [
    action,
    aspectRatio,
    canEnhancePrompt,
    clearPromptEnhanceChoices,
    durationS,
    effectiveResolution,
    generateAudio,
    inputImageId,
    isEnhancingPrompt,
    prompt,
    referenceMedia,
    selectedModel,
  ]);

  const applyPromptEnhanceCandidate = useCallback(
    (candidate: PromptEnhanceCandidate) => {
      setPrompt(candidate.prompt);
      setSelectedPromptEnhanceCandidateId(candidate.id);
      requestAnimationFrame(() => promptRef.current?.focus());
    },
    [],
  );

  const handlePromptChange = useCallback(
    (value: string) => {
      clearPromptEnhanceChoices();
      setPrompt(value);
    },
    [clearPromptEnhanceChoices],
  );

  const submitDisabledReason = useMemo(() => {
    if (createMut.isPending) return "正在提交";
    if (optionsQ.isLoading) return "正在读取配置";
    if (!options?.enabled) return options?.unavailable_reason ?? "功能未启用";
    if (!selectedModel) return "没有可用模型";
    if (!availableResolutions.includes(effectiveResolution)) return "当前模型不支持该分辨率";
    if (!prompt.trim()) return "先填写描述";
    if (action === "i2v" && !inputImageId.trim()) return "需要上传首帧或填写图片 ID";
    if (action === "reference" && referenceMedia.length === 0) {
      return "先添加参考素材";
    }
    if (estimate === null) return "缺少预扣估算";
    return "可以提交";
  }, [
    action,
    availableResolutions,
    createMut.isPending,
    estimate,
    inputImageId,
    options?.enabled,
    options?.unavailable_reason,
    optionsQ.isLoading,
    prompt,
    referenceMedia.length,
    effectiveResolution,
    selectedModel,
  ]);

  const canSubmit =
    Boolean(options?.enabled) &&
    Boolean(selectedModel) &&
    prompt.trim().length > 0 &&
    availableResolutions.includes(effectiveResolution) &&
    (action === "t2v" ||
      (action === "i2v" && inputImageId.trim().length > 0) ||
      (action === "reference" && referenceMedia.length > 0)) &&
    estimate !== null &&
    !createMut.isPending;
  const serviceEnabled = Boolean(options?.enabled);
  const serviceSummary = optionsQ.isLoading
    ? "读取视频服务配置"
    : serviceEnabled
      ? `${availableModels.length} 个模型可用`
      : options?.unavailable_reason ?? "需要先配置可用的视频供应商";
  const parameterProfile = `${effectiveResolution} · ${formatDurationLabel(durationS)}`;
  const sourceReady =
    action === "t2v" ||
    (action === "i2v" && inputImageId.trim().length > 0) ||
    (action === "reference" && referenceMedia.length > 0);
  const modelOptionValues = availableModels.map((item) => item.model);
  const storyboardModelOptionValues = (
    options?.models.filter((item) => item.actions.includes(storyboardAction)) ?? []
  ).map((item) => item.model);
  const durationOptionValues = (options?.durations_s ?? VIDEO_DURATION_OPTIONS).map(String);
  const aspectRatioOptionValues = options?.aspect_ratios ?? [
    "adaptive",
    "16:9",
    "9:16",
    "1:1",
  ];

  return (
    <div className="min-h-[100dvh] bg-[var(--bg-0)] text-[var(--fg-0)]">
      <div className="hidden md:block">
        <DesktopTopNav active="video" />
      </div>
      <input
        ref={referenceFileRef}
        type="file"
        accept={
          workspaceMode === "storyboard"
            ? "image/png,image/jpeg,image/webp"
            : "image/png,image/jpeg,image/webp,video/mp4,video/quicktime"
        }
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) referenceUploadMut.mutate(file);
          event.target.value = "";
        }}
      />
      <main className="lumen-studio-bg mx-auto flex w-full max-w-[1520px] flex-col gap-3 px-3 pb-32 pt-2 md:h-[calc(100dvh-3rem)] md:px-5 md:pb-4">
        <VideoWorkbenchHeader
          mode={workspaceMode === "storyboard" ? "高级故事板" : actionLabel(action)}
          profile={
            workspaceMode === "storyboard"
              ? `${storyboardShots.length} 镜头 · ${storyboardKeyframeApprovedCount} 批准帧 · ${storyboardGeneratedCount} 已成片`
              : parameterProfile
          }
          audio={generateAudio}
          enabled={serviceEnabled}
          loading={optionsQ.isLoading}
          activeCount={activeItems.length}
          completedCount={completedVideoItems.length}
          serviceSummary={serviceSummary}
          submitState={
            workspaceMode === "storyboard"
              ? storyboardSubmitDisabledReason
              : submitDisabledReason
          }
        />

        <WorkspaceModeSwitch
          value={workspaceMode}
          onChange={setWorkspaceMode}
          storyboardCount={storyboardShots.length}
          activeCount={activeItems.length}
        />

        {workspaceMode === "storyboard" ? (
          <StoryboardWorkspace
            title={storyboardTitle}
            stage={storyboardStage}
            idea={storyboardIdea}
            script={storyboardScript}
            scriptConfirmed={scriptConfirmed}
            scriptRevision={scriptRevision}
            scriptApprovedRevision={scriptApprovedRevision}
            scriptApprovedAt={scriptApprovedAt}
            style={storyboardStyle}
            assets={storyboardAssets}
            shots={storyboardShots}
            selectedShotId={selectedShotId}
            selectedShot={selectedShot}
            generationById={generationById}
            imageTaskById={storyboardImageTaskById}
            activeCount={activeItems.length}
            completedCount={storyboardGeneratedCount}
            assetReadyCount={storyboardAssetReadyCount}
            assetApprovedCount={storyboardAssetApprovedCount}
            keyframeReadyCount={storyboardKeyframeReadyCount}
            keyframeApprovedCount={storyboardKeyframeApprovedCount}
            staleKeyframeCount={storyboardStaleKeyframeCount}
            referenceMedia={referenceMedia}
            referenceUploading={referenceUploadMut.isPending}
            assetUrlInput={assetUrlInput}
            storyboardAction={storyboardAction}
            selectedModel={storyboardSelectedModel}
            modelOptions={storyboardModelOptionValues}
            resolution={storyboardEffectiveResolution}
            resolutionOptions={storyboardResolutionOptions}
            aspectRatio={aspectRatio}
            aspectRatioOptions={aspectRatioOptionValues}
            generateAudio={generateAudio}
            seed={seed}
            estimate={storyboardEstimate}
            submitReason={storyboardSubmitDisabledReason}
            canSubmitShot={canSubmitStoryboardShot}
            submitting={isSubmittingStoryboard}
            expandingScript={isExpandingScript}
            extractingAssets={isExtractingAssets}
            generatingAssetId={generatingAssetId}
            generatingKeyframeShotId={generatingKeyframeShotId}
            selectedShotPrompt={selectedShotPrompt}
            selectedShotKeyframePrompt={selectedShotKeyframePrompt}
            onTitleChange={setStoryboardTitle}
            onStageChange={setStoryboardStage}
            onIdeaChange={setStoryboardIdea}
            onScriptChange={handleStoryboardScriptChange}
            onStyleChange={setStoryboardStyle}
            onExpandIdea={expandStoryboardIdea}
            onConfirmScript={confirmStoryboardScript}
            onExtractAssets={extractStoryboardAssets}
            onAddAsset={addStoryboardAsset}
            onUpdateAsset={updateStoryboardAsset}
            onRemoveAsset={removeStoryboardAsset}
            onApproveAsset={approveStoryboardAsset}
            onRevokeAssetApproval={revokeStoryboardAssetApproval}
            onGenerateAssetImage={(id) => void generateStoryboardAssetImage(id)}
            onGenerateDraft={rebuildStoryboardFromScript}
            onAddShot={addStoryboardShot}
            onSelectShot={setSelectedShotId}
            onUpdateShot={updateStoryboardShot}
            onApproveShot={approveStoryboardShot}
            onDuplicateShot={duplicateStoryboardShot}
            onRemoveShot={removeStoryboardShot}
            onMoveShot={moveStoryboardShot}
            onGenerateKeyframe={(id) => void generateStoryboardShotKeyframe(id)}
            onApproveKeyframe={approveStoryboardShotKeyframe}
            onGenerateAllKeyframes={() => void generateAllStoryboardKeyframes()}
            onSubmitShot={() => void submitSelectedStoryboardShot()}
            onSubmitAll={() => void submitStoryboardQueue()}
            onUseSingleGenerator={applySelectedShotToSingleGenerator}
            onPreview={(item) => setSelectedVideoId(item.video.id)}
            onCancelGeneration={(item) => cancelMut.mutate(item.id)}
            onRetryGeneration={(item) => retryMut.mutate(item.id)}
            onCopyPrompt={() => {
              void navigator.clipboard?.writeText(selectedShotPrompt);
              toast.success("镜头提示词已复制");
            }}
            onReferenceUploadClick={() => referenceFileRef.current?.click()}
            onRemoveReference={(key) => {
              clearPromptEnhanceChoices();
              setReferenceMedia((prev) => prev.filter((ref) => ref._key !== key));
            }}
            onInsertReference={(label) => {
              if (!selectedShot) {
                insertReferenceTag(label);
                return;
              }
              updateStoryboardShot(selectedShot.id, {
                referenceNotes: appendReferenceNote(
                  selectedShot.referenceNotes,
                  label,
                ),
              });
              toast.success("已写入当前镜头参考说明");
            }}
            onAssetUrlInputChange={setAssetUrlInput}
            onAddAssetReference={addAssetReference}
            onModelChange={(value) => {
              clearPromptEnhanceChoices();
              setModel(value);
            }}
            onResolutionChange={(value) => {
              clearPromptEnhanceChoices();
              setResolution(value);
            }}
            onAspectRatioChange={(value) => {
              clearPromptEnhanceChoices();
              setAspectRatio(value);
            }}
            onGenerateAudioChange={(value) => {
              clearPromptEnhanceChoices();
              setGenerateAudio(value);
            }}
            onSeedChange={setSeed}
          />
        ) : (
        <div className="grid min-h-0 flex-1 gap-3 2xl:grid-cols-[minmax(0,1fr)_minmax(320px,380px)] 2xl:items-stretch">
          <section className="min-w-0">
            <Card
              variant="subtle"
              elevation={2}
              padding="none"
              className="flex h-full min-h-0 flex-col overflow-hidden border-[var(--border)]"
            >
              <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain p-3 sm:p-4">
                <div className="space-y-1.5">
                  <div className="grid min-w-0 grid-cols-[repeat(3,minmax(0,1fr))] rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-1">
                    {(Object.keys(MODE_COPY) as VideoAction[]).map((key) => (
                      <ModeCard
                        key={key}
                        actionKey={key}
                        selected={action === key}
                        onSelect={() => {
                          clearPromptEnhanceChoices();
                          setAction(key);
                          setModel(firstModelForAction(options, key));
                        }}
                      />
                    ))}
                  </div>
                  <div className="flex flex-wrap items-center justify-between gap-2 px-1 text-xs text-[var(--fg-2)]">
                    <span>{MODE_COPY[action].description}</span>
                    <span className="font-medium text-[var(--fg-1)]">{MODE_COPY[action].requirement}</span>
                  </div>
                </div>

                <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(320px,360px)] xl:items-start">
                  <div className="space-y-3">
                    {action === "i2v" && (
                      <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-2.5 shadow-[var(--shadow-1)]">
                        <input
                          ref={fileRef}
                          type="file"
                          accept="image/png,image/jpeg,image/webp"
                          className="hidden"
                          onChange={(event) => {
                            const file = event.target.files?.[0];
                            if (file) uploadMut.mutate(file);
                            event.target.value = "";
                          }}
                        />
                        <div className="grid gap-2 lg:grid-cols-[minmax(0,1fr)_minmax(220px,0.38fr)] lg:items-end">
                          <button
                            type="button"
                            onClick={() => fileRef.current?.click()}
                            disabled={uploadMut.isPending}
                            className="group flex min-h-14 items-center gap-3 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-0)]/72 p-3 text-left transition-[background-color,border-color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] disabled:pointer-events-none disabled:opacity-60"
                          >
                            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
                              {uploadMut.isPending ? (
                                <RefreshCw className="h-4 w-4 animate-spin" />
                              ) : (
                                <Upload className="h-4 w-4" />
                              )}
                            </span>
                            <span className="min-w-0">
                              <span className="block text-sm font-semibold text-[var(--fg-0)]">
                                {inputImageId ? "替换首帧" : "上传首帧图片"}
                              </span>
                              <span className="mt-1 block truncate text-xs font-medium text-[var(--fg-1)]">
                                {uploadedLabel || inputImageId
                                  ? uploadedLabel || "已填写图片 ID"
                                  : "尚未选择首帧"}
                              </span>
                            </span>
                          </button>
                          <label className="space-y-1.5">
                            <span className="type-caption text-[var(--fg-2)]">已有图片 ID</span>
                            <input
                              value={inputImageId}
                              onChange={(event) => {
                                clearPromptEnhanceChoices();
                                setInputImageId(event.target.value);
                                setUploadedLabel("");
                              }}
                              placeholder="image_id"
                              className="h-11 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 font-mono text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
                            />
                            <span className="block text-xs leading-5 text-[var(--fg-2)]">
                              从历史或接口复制 ID 时可直接粘贴。
                            </span>
                          </label>
                        </div>
                      </div>
                    )}

                    {action === "reference" && (
                      <div className="rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-2.5 shadow-[var(--shadow-1)]">
                        <input
                          ref={referenceFileRef}
                          type="file"
                          accept="image/png,image/jpeg,image/webp,video/mp4,video/quicktime"
                          className="hidden"
                          onChange={(event) => {
                            const file = event.target.files?.[0];
                            if (file) referenceUploadMut.mutate(file);
                            event.target.value = "";
                          }}
                        />
                        <div className="flex flex-wrap items-center gap-2">
                          <button
                            type="button"
                            onClick={() => referenceFileRef.current?.click()}
                            disabled={referenceUploadMut.isPending}
                            className="group inline-flex min-h-10 shrink-0 items-center gap-2 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-0)]/72 px-3 text-left transition-[background-color,border-color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] disabled:pointer-events-none disabled:opacity-60"
                          >
                            {referenceUploadMut.isPending ? (
                              <RefreshCw className="h-3.5 w-3.5 animate-spin text-[var(--accent)]" />
                            ) : (
                              <Upload className="h-3.5 w-3.5 text-[var(--accent)]" />
                            )}
                            <span className="text-sm font-semibold text-[var(--fg-0)]">
                              上传参考
                            </span>
                          </button>
                          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2.5 py-1 text-xs text-[var(--fg-2)]">
                            图片 {referenceMedia.filter((item) => item.kind === "image").length}/9
                          </span>
                          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2.5 py-1 text-xs text-[var(--fg-2)]">
                            视频 {referenceMedia.filter((item) => item.kind === "video").length}/3
                          </span>
                          <div className="flex w-full min-w-0 flex-wrap items-center gap-2 lg:w-auto lg:min-w-[360px] lg:flex-1">
                            <div className="relative min-w-[180px] flex-1">
                              <Tags className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[var(--fg-2)]" />
                              <input
                                value={assetUrlInput}
                                onChange={(event) =>
                                  setAssetUrlInput(event.target.value.toLowerCase())
                                }
                                onKeyDown={(event) => {
                                  if (event.key === "Enter") {
                                    event.preventDefault();
                                    addAssetReference();
                                  }
                                }}
                                placeholder="asset://asset-..."
                                className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] pl-9 pr-3 font-mono text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
                              />
                            </div>
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={!assetUrlInput.trim()}
                              onClick={addAssetReference}
                            >
                              添加官方素材
                            </Button>
                          </div>
                          <div className="flex min-w-[180px] flex-1 gap-2 overflow-x-auto py-1">
                            {referenceMedia.map((item) => (
                              <ReferenceChip
                                key={item._key}
                                item={item}
                                onInsert={() => insertReferenceTag(item.label)}
                                onRemove={() => {
                                  clearPromptEnhanceChoices();
                                  setReferenceMedia((prev) =>
                                    prev.filter((ref) => ref._key !== item._key),
                                  );
                                }}
                              />
                            ))}
                            {referenceMedia.length === 0 && (
                              <span className="shrink-0 rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-1)]/70 px-3 py-2 text-xs text-[var(--fg-2)]">
                                未添加参考素材
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                    )}

                    <div className="space-y-2">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="type-caption text-[var(--fg-2)]">提示词</span>
                        <div className="flex items-center gap-2">
                          <span className="text-xs tabular-nums text-[var(--fg-2)]">
                            {prompt.length.toLocaleString()} / 10,000
                          </span>
                          <Button
                            variant="outline"
                            size="sm"
                            loading={isEnhancingPrompt}
                            disabled={!canEnhancePrompt}
                            onClick={() => void enhancePromptAction()}
                            leftIcon={<PencilLine className="h-3.5 w-3.5" />}
                          >
                            优化
                          </Button>
                        </div>
                      </div>
                      <textarea
                        ref={promptRef}
                        value={prompt}
                        onChange={(event) => handlePromptChange(event.target.value)}
                        readOnly={isEnhancingPrompt}
                        rows={6}
                        maxLength={10000}
                        placeholder="写清主体、动作、镜头运动、节奏、参考素材怎么使用，以及不要出现的内容。"
                        className={cn(
                          "min-h-[190px] w-full resize-none rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/80 p-3 text-sm leading-6 text-[var(--fg-0)] outline-none transition-[border-color,box-shadow] focus:border-[var(--accent)]/60 focus:shadow-[var(--ring)] placeholder:text-[var(--fg-2)] sm:min-h-[240px]",
                          isEnhancingPrompt && "cursor-wait border-[var(--accent)]/50",
                        )}
                      />
                      {(isEnhancingPrompt ||
                        promptEnhancePreview.trim() ||
                        promptEnhanceCandidates.length > 0) && (
                        <PromptEnhanceChooser
                          loading={isEnhancingPrompt}
                          preview={promptEnhancePreview}
                          candidates={promptEnhanceCandidates}
                          selectedId={selectedPromptEnhanceCandidateId}
                          onSelect={applyPromptEnhanceCandidate}
                          onDismiss={clearPromptEnhanceChoices}
                        />
                      )}
                      <div className="-mx-1 flex gap-2 overflow-x-auto px-1 pb-1">
                        {PROMPT_CHIPS.map((chip) => (
                          <button
                            key={chip}
                            type="button"
                            disabled={isEnhancingPrompt}
                            onClick={() => insertPromptText(chip)}
                            className="shrink-0 rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-3 py-1.5 text-xs text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)] disabled:pointer-events-none disabled:opacity-50"
                          >
                            {chip}
                          </button>
                        ))}
                      </div>
                    </div>
                  </div>

                  <VideoParameterPanel
                    className="order-first xl:sticky xl:top-4 xl:order-none"
                    selectedModel={selectedModel}
                    modelOptions={modelOptionValues}
                    durationS={durationS}
                    durationOptions={durationOptionValues}
                    resolution={effectiveResolution}
                    resolutionOptions={availableResolutions}
                    aspectRatio={aspectRatio}
                    aspectRatioOptions={aspectRatioOptionValues}
                    seed={seed}
                    generateAudio={generateAudio}
                    estimate={estimate}
                    canSubmit={canSubmit}
                    reason={submitDisabledReason}
                    loading={createMut.isPending}
                    sourceReady={sourceReady}
                    onSubmit={() => createMut.mutate()}
                    onModelChange={(value) => {
                      clearPromptEnhanceChoices();
                      setModel(value);
                    }}
                    onDurationChange={(value) => {
                      clearPromptEnhanceChoices();
                      setDurationS(Number(value));
                    }}
                    onResolutionChange={(value) => {
                      clearPromptEnhanceChoices();
                      setResolution(value);
                    }}
                    onAspectRatioChange={(value) => {
                      clearPromptEnhanceChoices();
                      setAspectRatio(value);
                    }}
                    onSeedChange={setSeed}
                    onGenerateAudioChange={(value) => {
                      clearPromptEnhanceChoices();
                      setGenerateAudio(value);
                    }}
                  />
                </div>
              </div>
            </Card>
          </section>

          <section className="min-w-0 space-y-4 2xl:sticky 2xl:top-4">
            <Card
              variant="subtle"
              elevation={2}
              padding="none"
              className="flex max-h-[720px] min-h-[420px] flex-col overflow-hidden border-[var(--border)] 2xl:max-h-[calc(100dvh-5rem)]"
            >
              <div className="relative flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/74 p-3 sm:p-4">
                <span aria-hidden="true" className="absolute left-0 top-3 h-7 w-1 rounded-r-full bg-[var(--accent)]" />
                <div>
                  <div className="flex items-center gap-2">
                    <Clapperboard className="h-4 w-4 text-[var(--fg-2)]" />
                    <p className="type-card-title">任务</p>
                  </div>
                  <p className="mt-1 text-xs text-[var(--fg-2)]">
                    进行中与历史记录
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--fg-2)]">
                  <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 tabular-nums">
                    {activeItems.length} 活跃
                  </span>
                  <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 tabular-nums">
                    {historyQ.isLoading
                      ? "读取中"
                      : `${settledHistoryItems.length}${historyQ.hasNextPage ? "+" : ""} 历史`}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void historyQ.refetch()}
                    leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
                  >
                    刷新
                  </Button>
                </div>
              </div>
              <div className="min-h-0 flex-1 space-y-5 overflow-y-auto overscroll-contain p-4 pr-3 sm:p-5 sm:pr-4">
                {activeItems.length > 0 && (
                  <section className="space-y-3">
                    <div className="flex items-center justify-between gap-3">
                      <p className="type-caption text-[var(--fg-2)]">正在进行</p>
                      <span className="text-xs tabular-nums text-[var(--fg-2)]">
                        {activeItems.length} 条
                      </span>
                    </div>
                    <div className="grid gap-3">
                      {activeItems.map((item) => (
                        <TaskRow
                          key={item.id}
                          item={item}
                          onCancel={() => cancelMut.mutate(item.id)}
                          onRetry={() => retryMut.mutate(item.id)}
                          onCopy={() => {
                            void navigator.clipboard?.writeText(item.prompt);
                            toast.success("描述已复制");
                          }}
                          onUseDraft={() => loadAsDraft(item)}
                          showPreview={false}
                        />
                      ))}
                    </div>
                  </section>
                )}

                <section className="space-y-3">
                  <div className="flex items-center justify-between gap-3">
                    <p className="type-caption text-[var(--fg-2)]">历史记录</p>
                    <span className="text-xs tabular-nums text-[var(--fg-2)]">
                      {historyQ.isLoading
                        ? "读取中"
                        : `${filteredHistoryItems.length}${historyQ.hasNextPage ? "+" : ""} 条`}
                    </span>
                  </div>
                  <HistoryFilterTabs
                    value={historyFilter}
                    counts={{
                      all: settledHistoryItems.length,
                      succeeded: succeededHistoryItems.length,
                      failed: failedHistoryItems.length,
                    }}
                    loading={historyQ.isLoading}
                    onChange={setHistoryFilter}
                  />
                  <div className="grid gap-3">
                    {filteredHistoryItems.map((item) => (
                      <TaskRow
                        key={item.id}
                        item={item}
                        onCancel={() => cancelMut.mutate(item.id)}
                        onRetry={() => retryMut.mutate(item.id)}
                        onCopy={() => {
                          void navigator.clipboard?.writeText(item.prompt);
                          toast.success("描述已复制");
                        }}
                        onUseDraft={() => loadAsDraft(item)}
                        onDelete={() => item.video && deleteMut.mutate(item.video.id)}
                        onPreview={hasVideo(item) ? () => setSelectedVideoId(item.video.id) : undefined}
                        selected={selectedVideoId === item.video?.id}
                        showPreview={false}
                      />
                    ))}
                    {filteredHistoryItems.length === 0 && (
                      <EmptyPanel
                        icon={<Film className="h-5 w-5" />}
                        title={
                          historyQ.isLoading
                            ? "读取中"
                            : `暂无${videoHistoryFilterLabel(historyFilter)}记录`
                        }
                        description={
                          activeItems.length > 0
                            ? "当前任务完成后会进入历史。"
                            : historyFilter === "all"
                              ? "提交记录会保留状态、参数和结果。"
                              : "切换标签可查看其他状态的记录。"
                        }
                      />
                    )}
                    {historyQ.hasNextPage && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="w-full"
                        loading={historyQ.isFetchingNextPage}
                        onClick={() => void historyQ.fetchNextPage()}
                        leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
                      >
                        {historyQ.isFetchingNextPage ? "加载中" : "加载更早记录"}
                      </Button>
                    )}
                  </div>
                </section>
              </div>
            </Card>
          </section>
        </div>
        )}
      </main>
      {playbackVideoItem && (
        <VideoPreviewDialog
          item={playbackVideoItem}
          onClose={() => setSelectedVideoId("")}
          onUseDraft={() => loadAsDraft(playbackVideoItem)}
          onRetry={() => retryMut.mutate(playbackVideoItem.id)}
          onCopy={() => {
            void navigator.clipboard?.writeText(playbackVideoItem.prompt);
            toast.success("描述已复制");
          }}
          onDelete={() => deleteMut.mutate(playbackVideoItem.video.id)}
        />
      )}
      <div className="md:hidden">
        <MobileTabBar />
      </div>
    </div>
  );
}

function WorkspaceModeSwitch({
  value,
  onChange,
  storyboardCount,
  activeCount,
}: {
  value: VideoWorkspaceMode;
  onChange: (value: VideoWorkspaceMode) => void;
  storyboardCount: number;
  activeCount: number;
}) {
  const tabs: Array<{
    value: VideoWorkspaceMode;
    label: string;
    detail: string;
    icon: React.ReactNode;
  }> = [
    {
      value: "single",
      label: "单条生成",
      detail: activeCount > 0 ? `${activeCount} 进行中` : "原视频生成器",
      icon: <Film className="h-4 w-4" />,
    },
    {
      value: "storyboard",
      label: "高级故事板",
      detail: `${storyboardCount} 个镜头 · 可增减`,
      icon: <ClipboardList className="h-4 w-4" />,
    },
  ];

  return (
    <div className="grid w-full min-w-0 max-w-[420px] shrink-0 grid-cols-2 gap-1 overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-1">
      {tabs.map((tab) => {
        const active = tab.value === value;
        return (
          <button
            key={tab.value}
            type="button"
            onClick={() => onChange(tab.value)}
            aria-pressed={active}
            className={cn(
              "flex min-h-11 min-w-0 items-center justify-center gap-2 rounded-[var(--radius-control)] px-2 text-center transition-[background-color,border-color,color,box-shadow] sm:px-3 sm:text-left",
              active
                ? "bg-[var(--bg-2)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                : "text-[var(--fg-2)] hover:bg-[var(--bg-1)] hover:text-[var(--fg-0)]",
            )}
          >
            <span
              className={cn(
                "hidden h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] border sm:flex",
                active
                  ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]"
                  : "border-[var(--border-subtle)] bg-[var(--bg-1)]",
              )}
            >
              {tab.icon}
            </span>
            <span className="min-w-0">
              <span className="block truncate text-[13px] font-semibold sm:text-sm">{tab.label}</span>
              <span className="hidden truncate text-[11px] text-[var(--fg-2)] sm:block">
                {tab.detail}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function StoryboardWorkspace({
  title,
  stage,
  idea,
  script,
  scriptConfirmed,
  scriptRevision,
  scriptApprovedRevision,
  scriptApprovedAt,
  style,
  assets,
  shots,
  selectedShotId,
  selectedShot,
  generationById,
  imageTaskById,
  activeCount,
  completedCount,
  assetReadyCount,
  assetApprovedCount,
  keyframeReadyCount,
  keyframeApprovedCount,
  staleKeyframeCount,
  referenceMedia,
  referenceUploading,
  assetUrlInput,
  storyboardAction,
  selectedModel,
  modelOptions,
  resolution,
  resolutionOptions,
  aspectRatio,
  aspectRatioOptions,
  generateAudio,
  seed,
  estimate,
  submitReason,
  canSubmitShot,
  submitting,
  expandingScript,
  extractingAssets,
  generatingAssetId,
  generatingKeyframeShotId,
  selectedShotPrompt,
  selectedShotKeyframePrompt,
  onTitleChange,
  onStageChange,
  onIdeaChange,
  onScriptChange,
  onStyleChange,
  onExpandIdea,
  onConfirmScript,
  onExtractAssets,
  onAddAsset,
  onUpdateAsset,
  onRemoveAsset,
  onApproveAsset,
  onRevokeAssetApproval,
  onGenerateAssetImage,
  onGenerateDraft,
  onAddShot,
  onSelectShot,
  onUpdateShot,
  onApproveShot,
  onDuplicateShot,
  onRemoveShot,
  onMoveShot,
  onGenerateKeyframe,
  onApproveKeyframe,
  onGenerateAllKeyframes,
  onSubmitShot,
  onSubmitAll,
  onUseSingleGenerator,
  onPreview,
  onCancelGeneration,
  onRetryGeneration,
  onCopyPrompt,
  onReferenceUploadClick,
  onRemoveReference,
  onInsertReference,
  onAssetUrlInputChange,
  onAddAssetReference,
  onModelChange,
  onResolutionChange,
  onAspectRatioChange,
  onGenerateAudioChange,
  onSeedChange,
}: {
  title: string;
  stage: StoryboardWorkflowStage;
  idea: string;
  script: string;
  scriptConfirmed: boolean;
  scriptRevision: number;
  scriptApprovedRevision: number;
  scriptApprovedAt: string;
  style: string;
  assets: StoryboardReferenceAsset[];
  shots: StoryboardShot[];
  selectedShotId: string;
  selectedShot?: StoryboardShot;
  generationById: Map<string, VideoGenerationOut>;
  imageTaskById: Map<string, BackendGeneration>;
  activeCount: number;
  completedCount: number;
  assetReadyCount: number;
  assetApprovedCount: number;
  keyframeReadyCount: number;
  keyframeApprovedCount: number;
  staleKeyframeCount: number;
  referenceMedia: ReferenceDraft[];
  referenceUploading: boolean;
  assetUrlInput: string;
  storyboardAction: VideoAction;
  selectedModel: string;
  modelOptions: string[];
  resolution: string;
  resolutionOptions: string[];
  aspectRatio: string;
  aspectRatioOptions: string[];
  generateAudio: boolean;
  seed: string;
  estimate: { tokens: number; micro: number } | null;
  submitReason: string;
  canSubmitShot: boolean;
  submitting: boolean;
  expandingScript: boolean;
  extractingAssets: boolean;
  generatingAssetId: string;
  generatingKeyframeShotId: string;
  selectedShotPrompt: string;
  selectedShotKeyframePrompt: string;
  onTitleChange: (value: string) => void;
  onStageChange: (value: StoryboardWorkflowStage) => void;
  onIdeaChange: (value: string) => void;
  onScriptChange: (value: string) => void;
  onStyleChange: (value: string) => void;
  onExpandIdea: () => void;
  onConfirmScript: () => void;
  onExtractAssets: () => void;
  onAddAsset: (kind: StoryboardAssetKind) => void;
  onUpdateAsset: (id: string, patch: StoryboardReferenceAssetPatch) => void;
  onRemoveAsset: (id: string) => void;
  onApproveAsset: (id: string) => void;
  onRevokeAssetApproval: (id: string) => void;
  onGenerateAssetImage: (id: string) => void;
  onGenerateDraft: () => void;
  onAddShot: () => void;
  onSelectShot: (id: string) => void;
  onUpdateShot: (id: string, patch: StoryboardShotPatch) => void;
  onApproveShot: (id: string) => void;
  onDuplicateShot: (id: string) => void;
  onRemoveShot: (id: string) => void;
  onMoveShot: (id: string, direction: -1 | 1) => void;
  onGenerateKeyframe: (id: string) => void;
  onApproveKeyframe: (id: string) => void;
  onGenerateAllKeyframes: () => void;
  onSubmitShot: () => void;
  onSubmitAll: () => void;
  onUseSingleGenerator: () => void;
  onPreview: (item: VideoGenerationWithVideo) => void;
  onCancelGeneration: (item: VideoGenerationOut) => void;
  onRetryGeneration: (item: VideoGenerationOut) => void;
  onCopyPrompt: () => void;
  onReferenceUploadClick: () => void;
  onRemoveReference: (key: string) => void;
  onInsertReference: (label: string) => void;
  onAssetUrlInputChange: (value: string) => void;
  onAddAssetReference: () => void;
  onModelChange: (value: string) => void;
  onResolutionChange: (value: string) => void;
  onAspectRatioChange: (value: string) => void;
  onGenerateAudioChange: (value: boolean) => void;
  onSeedChange: (value: string) => void;
}) {
  const selectedGeneration =
    selectedShot?.generationId != null
      ? generationById.get(selectedShot.generationId)
      : undefined;
  const selectedKeyframeTask =
    selectedShot?.keyframeGenerationId != null
      ? imageTaskById.get(selectedShot.keyframeGenerationId)
      : undefined;
  const selectedShotAssets = selectedShot
    ? assetsForShot(selectedShot, assets)
    : [];
  const externalReferenceImageIds = storyboardReferenceImageIds(referenceMedia);
  const selectedShotKeyframeStale = selectedShot
    ? storyboardShotKeyframeStale(selectedShot, assets, externalReferenceImageIds)
    : false;
  const totalDuration = shots.reduce((sum, shot) => sum + shot.durationS, 0);
  const runningStoryboardCount = shots.filter((shot) => {
    const gen = shot.generationId ? generationById.get(shot.generationId) : undefined;
    return gen ? isActiveVideo(gen) : false;
  }).length;
  const runningImageCount = Array.from(imageTaskById.values()).filter(
    isActiveImageTask,
  ).length;

  return (
    <div className="grid min-h-0 flex-1 gap-3 xl:grid-cols-[minmax(260px,320px)_minmax(0,1fr)_minmax(300px,360px)]">
      <Card
        variant="subtle"
        elevation={2}
        padding="none"
        className="min-h-0 overflow-hidden border-[var(--border)]"
      >
        <StoryboardProductionPanel
          title={title}
          stage={stage}
          idea={idea}
          script={script}
          scriptConfirmed={scriptConfirmed}
          scriptRevision={scriptRevision}
          scriptApprovedRevision={scriptApprovedRevision}
          scriptApprovedAt={scriptApprovedAt}
          style={style}
          assets={assets}
          shots={shots}
          totalDuration={totalDuration}
          completedCount={completedCount}
          assetReadyCount={assetReadyCount}
          assetApprovedCount={assetApprovedCount}
          keyframeReadyCount={keyframeReadyCount}
          keyframeApprovedCount={keyframeApprovedCount}
          staleKeyframeCount={staleKeyframeCount}
          runningImageCount={runningImageCount}
          imageTaskById={imageTaskById}
          expandingScript={expandingScript}
          extractingAssets={extractingAssets}
          generatingAssetId={generatingAssetId}
          onTitleChange={onTitleChange}
          onStageChange={onStageChange}
          onIdeaChange={onIdeaChange}
          onScriptChange={onScriptChange}
          onStyleChange={onStyleChange}
          onExpandIdea={onExpandIdea}
          onConfirmScript={onConfirmScript}
          onExtractAssets={onExtractAssets}
          onAddAsset={onAddAsset}
          onUpdateAsset={onUpdateAsset}
          onRemoveAsset={onRemoveAsset}
          onApproveAsset={onApproveAsset}
          onRevokeAssetApproval={onRevokeAssetApproval}
          onGenerateAssetImage={onGenerateAssetImage}
          onGenerateDraft={onGenerateDraft}
        />
      </Card>

      <Card
        variant="subtle"
        elevation={2}
        padding="none"
        className="min-h-0 overflow-hidden border-[var(--border)]"
      >
        <div className="flex h-full min-h-0 flex-col">
          <header className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/74 p-3">
            <div className="flex min-w-0 items-center gap-2">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
                <Layers3 className="h-4 w-4" />
              </span>
              <div className="min-w-0">
                <p className="type-card-title">分镜编排</p>
                <p className="mt-0.5 truncate text-xs text-[var(--fg-2)]">
                  每个镜头按 15 秒内片段提交
                </p>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2.5 py-1 text-xs text-[var(--fg-2)]">
                {runningStoryboardCount} 镜头运行中
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={onAddShot}
                leftIcon={<Plus className="h-3.5 w-3.5" />}
              >
                加镜头
              </Button>
            </div>
          </header>
          <div className="grid min-h-0 flex-1 gap-0 lg:grid-cols-[minmax(250px,330px)_minmax(0,1fr)]">
            <div className="min-h-0 border-b border-[var(--border)] lg:border-b-0 lg:border-r">
              <div className="flex max-h-72 gap-2 overflow-x-auto p-3 lg:max-h-none lg:h-full lg:flex-col lg:overflow-y-auto lg:overflow-x-hidden">
                {shots.map((shot, index) => {
                  const generation = shot.generationId
                    ? generationById.get(shot.generationId)
                    : undefined;
                  return (
                    <StoryboardShotCard
                      key={shot.id}
                      shot={shot}
                      index={index}
                      total={shots.length}
                      selected={shot.id === selectedShotId}
                      generation={generation}
                      keyframeTask={
                        shot.keyframeGenerationId
                          ? imageTaskById.get(shot.keyframeGenerationId)
                          : undefined
                      }
                      keyframeStale={storyboardShotKeyframeStale(
                        shot,
                        assets,
                        externalReferenceImageIds,
                      )}
                      boundAssetCount={assetIdsForShot(shot, assets).length}
                      onSelect={() => onSelectShot(shot.id)}
                      onDuplicate={() => onDuplicateShot(shot.id)}
                      onRemove={() => onRemoveShot(shot.id)}
                      onMoveUp={() => onMoveShot(shot.id, -1)}
                      onMoveDown={() => onMoveShot(shot.id, 1)}
                    />
                  );
                })}
              </div>
            </div>
            <StoryboardShotEditor
              shot={selectedShot}
              generation={selectedGeneration}
              keyframeTask={selectedKeyframeTask}
              assets={assets}
              selectedAssets={selectedShotAssets}
              referenceImageCount={externalReferenceImageIds.length}
              keyframeStale={selectedShotKeyframeStale}
              prompt={selectedShotPrompt}
              keyframePrompt={selectedShotKeyframePrompt}
              canSubmit={canSubmitShot}
              submitting={submitting}
              generatingKeyframe={generatingKeyframeShotId === selectedShot?.id}
              submitReason={submitReason}
              onUpdate={onUpdateShot}
              onApproveShot={() => {
                if (selectedShot) onApproveShot(selectedShot.id);
              }}
              onGenerateKeyframe={() => {
                if (selectedShot) onGenerateKeyframe(selectedShot.id);
              }}
              onApproveKeyframe={() => {
                if (selectedShot) onApproveKeyframe(selectedShot.id);
              }}
              onGenerateAllKeyframes={onGenerateAllKeyframes}
              onSubmit={onSubmitShot}
              onSubmitAll={onSubmitAll}
              onUseSingleGenerator={onUseSingleGenerator}
              onCopyPrompt={onCopyPrompt}
              onPreview={onPreview}
              onCancelGeneration={onCancelGeneration}
              onRetryGeneration={onRetryGeneration}
            />
          </div>
        </div>
      </Card>

      <StoryboardSidePanel
        action={storyboardAction}
        selectedModel={selectedModel}
        modelOptions={modelOptions}
        resolution={resolution}
        resolutionOptions={resolutionOptions}
        aspectRatio={aspectRatio}
        aspectRatioOptions={aspectRatioOptions}
        generateAudio={generateAudio}
        seed={seed}
        estimate={estimate}
        submitReason={submitReason}
        activeCount={activeCount}
        referenceMedia={referenceMedia}
        referenceUploading={referenceUploading}
        assetUrlInput={assetUrlInput}
        selectedGeneration={selectedGeneration}
        onModelChange={onModelChange}
        onResolutionChange={onResolutionChange}
        onAspectRatioChange={onAspectRatioChange}
        onGenerateAudioChange={onGenerateAudioChange}
        onSeedChange={onSeedChange}
        onReferenceUploadClick={onReferenceUploadClick}
        onRemoveReference={onRemoveReference}
        onInsertReference={onInsertReference}
        onAssetUrlInputChange={onAssetUrlInputChange}
        onAddAssetReference={onAddAssetReference}
        onPreview={onPreview}
        onCancelGeneration={onCancelGeneration}
        onRetryGeneration={onRetryGeneration}
      />
    </div>
  );
}

const STORYBOARD_STAGE_COPY: Array<{
  id: StoryboardWorkflowStage;
  label: string;
  detail: string;
}> = [
  { id: "idea", label: "想法", detail: "扩写" },
  { id: "script", label: "脚本", detail: "确认" },
  { id: "assets", label: "人物/场景", detail: "设定图" },
  { id: "shots", label: "分镜", detail: "拆分" },
  { id: "keyframes", label: "分镜图", detail: "逐镜头" },
  { id: "videos", label: "视频", detail: "图生片段" },
];

function StoryboardProductionPanel({
  title,
  stage,
  idea,
  script,
  scriptConfirmed,
  scriptRevision,
  scriptApprovedRevision,
  scriptApprovedAt,
  style,
  assets,
  shots,
  totalDuration,
  completedCount,
  assetReadyCount,
  assetApprovedCount,
  keyframeReadyCount,
  keyframeApprovedCount,
  staleKeyframeCount,
  runningImageCount,
  imageTaskById,
  expandingScript,
  extractingAssets,
  generatingAssetId,
  onTitleChange,
  onStageChange,
  onIdeaChange,
  onScriptChange,
  onStyleChange,
  onExpandIdea,
  onConfirmScript,
  onExtractAssets,
  onAddAsset,
  onUpdateAsset,
  onRemoveAsset,
  onApproveAsset,
  onRevokeAssetApproval,
  onGenerateAssetImage,
  onGenerateDraft,
}: {
  title: string;
  stage: StoryboardWorkflowStage;
  idea: string;
  script: string;
  scriptConfirmed: boolean;
  scriptRevision: number;
  scriptApprovedRevision: number;
  scriptApprovedAt: string;
  style: string;
  assets: StoryboardReferenceAsset[];
  shots: StoryboardShot[];
  totalDuration: number;
  completedCount: number;
  assetReadyCount: number;
  assetApprovedCount: number;
  keyframeReadyCount: number;
  keyframeApprovedCount: number;
  staleKeyframeCount: number;
  runningImageCount: number;
  imageTaskById: Map<string, BackendGeneration>;
  expandingScript: boolean;
  extractingAssets: boolean;
  generatingAssetId: string;
  onTitleChange: (value: string) => void;
  onStageChange: (value: StoryboardWorkflowStage) => void;
  onIdeaChange: (value: string) => void;
  onScriptChange: (value: string) => void;
  onStyleChange: (value: string) => void;
  onExpandIdea: () => void;
  onConfirmScript: () => void;
  onExtractAssets: () => void;
  onAddAsset: (kind: StoryboardAssetKind) => void;
  onUpdateAsset: (id: string, patch: StoryboardReferenceAssetPatch) => void;
  onRemoveAsset: (id: string) => void;
  onApproveAsset: (id: string) => void;
  onRevokeAssetApproval: (id: string) => void;
  onGenerateAssetImage: (id: string) => void;
  onGenerateDraft: () => void;
}) {
  const characters = assets.filter((asset) => asset.kind === "character");
  const scenes = assets.filter((asset) => asset.kind === "scene");
  const props = assets.filter((asset) => asset.kind === "prop");
  const shotApprovedCount = shots.filter((shot) => shot.approved).length;
  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="border-b border-[var(--border-subtle)] bg-[var(--bg-1)]/74 p-3">
        <div className="flex items-center gap-2">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
            <FileText className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <p className="type-card-title">故事版预生产</p>
            <p className="mt-0.5 truncate text-xs text-[var(--fg-2)]">
              想法到脚本，再到一致性设定和分镜图
            </p>
          </div>
        </div>
        <div className="mt-3 grid grid-cols-3 gap-1.5">
          {STORYBOARD_STAGE_COPY.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onStageChange(item.id)}
              className={cn(
                "min-w-0 rounded-[var(--radius-control)] border px-2 py-1.5 text-left transition-colors",
                stage === item.id
                  ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]"
                  : "border-[var(--border-subtle)] bg-[var(--bg-0)] text-[var(--fg-2)] hover:border-[var(--border)]",
              )}
            >
              <span className="block truncate text-xs font-semibold">{item.label}</span>
              <span className="block truncate text-[10px]">{item.detail}</span>
            </button>
          ))}
        </div>
      </header>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain p-3">
        <section className="grid grid-cols-2 gap-2">
          <StoryboardGateMetric
            label="脚本锁定"
            value={scriptConfirmed ? `v${scriptApprovedRevision}` : `草稿 v${scriptRevision}`}
            state={scriptConfirmed ? "done" : "draft"}
          />
          <StoryboardGateMetric
            label="设定批准"
            value={`${assetApprovedCount}/${assets.length}`}
            state={assetApprovedCount === assets.length && assets.length > 0 ? "done" : "draft"}
          />
          <StoryboardGateMetric
            label="镜头批准"
            value={`${shotApprovedCount}/${shots.length}`}
            state={shotApprovedCount === shots.length && shots.length > 0 ? "done" : "draft"}
          />
          <StoryboardGateMetric
            label="关键帧批准"
            value={
              staleKeyframeCount > 0
                ? `${staleKeyframeCount} 过期`
                : `${keyframeApprovedCount}/${shots.length}`
            }
            state={
              staleKeyframeCount > 0
                ? "warning"
                : keyframeApprovedCount === shots.length && shots.length > 0
                  ? "done"
                  : "draft"
            }
          />
        </section>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">项目名</span>
          <input
            value={title}
            onChange={(event) => onTitleChange(event.target.value)}
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
          />
        </label>

        <section className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/58 p-3">
          <div className="flex items-center justify-between gap-2">
            <p className="type-caption text-[var(--fg-2)]">想法</p>
            <Button
              variant="outline"
              size="sm"
              loading={expandingScript}
              onClick={onExpandIdea}
              leftIcon={<Sparkles className="h-3.5 w-3.5" />}
            >
              AI 扩写脚本
            </Button>
          </div>
          <textarea
            value={idea}
            onChange={(event) => onIdeaChange(event.target.value)}
            rows={4}
            className="min-h-24 w-full resize-none rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] p-3 text-sm leading-6 text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
          />
        </section>

        <section className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/58 p-3">
          <div className="flex items-center justify-between gap-2">
            <p className="type-caption text-[var(--fg-2)]">脚本</p>
            <span
              className={cn(
                "rounded-full border px-2 py-1 text-[11px]",
                scriptConfirmed
                  ? "border-success-border bg-success-soft text-success"
                  : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-2)]",
              )}
            >
              {scriptConfirmed ? `已锁定 v${scriptApprovedRevision}` : `草稿 v${scriptRevision}`}
            </span>
          </div>
          {scriptApprovedAt && (
            <p className="text-[11px] text-[var(--fg-2)]">
              锁定时间 {new Date(scriptApprovedAt).toLocaleString()}
            </p>
          )}
          <textarea
            value={script}
            onChange={(event) => onScriptChange(event.target.value)}
            rows={7}
            className="min-h-40 w-full resize-none rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] p-3 text-sm leading-6 text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
          />
          <div className="grid grid-cols-2 gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={onExpandIdea}
              loading={expandingScript}
              leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
            >
              继续优化
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={onConfirmScript}
              leftIcon={<CircleCheck className="h-3.5 w-3.5" />}
            >
              锁定脚本
            </Button>
          </div>
        </section>

        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">全片视觉连续性</span>
          <textarea
            value={style}
            onChange={(event) => onStyleChange(event.target.value)}
            rows={4}
            className="min-h-24 w-full resize-none rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-3 text-sm leading-6 text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
          />
        </label>

        <section className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/58 p-3">
          <div className="flex items-center justify-between gap-2">
            <div>
              <p className="type-caption text-[var(--fg-2)]">人物 / 场景 / 道具设定图</p>
              <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                {assetReadyCount}/{assets.length} 已有图 · {assetApprovedCount} 已批准 · {runningImageCount} 图像任务
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              loading={extractingAssets}
              onClick={onExtractAssets}
              leftIcon={<Tags className="h-3.5 w-3.5" />}
            >
              AI 提取
            </Button>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => onAddAsset("character")}
              leftIcon={<Plus className="h-3.5 w-3.5" />}
            >
              人物
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => onAddAsset("scene")}
              leftIcon={<Plus className="h-3.5 w-3.5" />}
            >
              场景
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => onAddAsset("prop")}
              leftIcon={<Plus className="h-3.5 w-3.5" />}
            >
              道具
            </Button>
          </div>
          <div className="space-y-2">
            {[...characters, ...scenes, ...props].map((asset) => (
              <StoryboardAssetCard
                key={asset.id}
                asset={asset}
                task={
                  asset.generationId ? imageTaskById.get(asset.generationId) : undefined
                }
                generating={generatingAssetId === asset.id}
                onUpdate={(patch) => onUpdateAsset(asset.id, patch)}
                onRemove={() => onRemoveAsset(asset.id)}
                onApprove={() => onApproveAsset(asset.id)}
                onRevokeApproval={() => onRevokeAssetApproval(asset.id)}
                onGenerate={() => onGenerateAssetImage(asset.id)}
              />
            ))}
          </div>
        </section>

        <div className="grid grid-cols-3 gap-2">
          <StoryboardMetric label="镜头" value={shots.length} />
          <StoryboardMetric label="已出图" value={`${keyframeReadyCount}/${shots.length}`} />
          <StoryboardMetric label="已成片" value={completedCount} />
        </div>
        <StoryboardMetric label="总时长" value={`${totalDuration}s`} />
        <Button
          variant="primary"
          size="sm"
          className="w-full"
          onClick={onGenerateDraft}
          leftIcon={<ListChecks className="h-3.5 w-3.5" />}
        >
          按确认脚本拆分镜
        </Button>
      </div>
    </div>
  );
}

function StoryboardAssetCard({
  asset,
  task,
  generating,
  onUpdate,
  onRemove,
  onApprove,
  onRevokeApproval,
  onGenerate,
}: {
  asset: StoryboardReferenceAsset;
  task?: BackendGeneration;
  generating: boolean;
  onUpdate: (patch: StoryboardReferenceAssetPatch) => void;
  onRemove: () => void;
  onApprove: () => void;
  onRevokeApproval: () => void;
  onGenerate: () => void;
}) {
  const progress = imageTaskProgress(task);
  return (
    <article className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/70 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex flex-wrap gap-1.5">
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 text-[11px] text-[var(--fg-2)]">
            {storyboardAssetLabel(asset.kind)}
          </span>
          <span
            className={cn(
              "rounded-full border px-2 py-1 text-[11px]",
              asset.approved
                ? "border-success-border bg-success-soft text-success"
                : "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]",
            )}
          >
            {asset.approved ? `已批准 v${asset.revision}` : `草稿 v${asset.revision}`}
          </span>
        </div>
        <button
          type="button"
          onClick={onRemove}
          aria-label="删除参考资产"
          className="inline-flex h-7 w-7 items-center justify-center rounded-[var(--radius-control)] text-[var(--fg-2)] transition-colors hover:bg-[var(--bg-2)] hover:text-danger"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
      {asset.imageUrl && (
        <img
          src={asset.imageUrl}
          alt={asset.name}
          className="aspect-video w-full rounded-[var(--radius-control)] border border-[var(--border)] object-cover"
        />
      )}
      <div className="grid gap-2">
        <input
          value={asset.name}
          onChange={(event) => onUpdate({ name: event.target.value })}
          className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-2.5 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
        />
        <input
          value={asset.role}
          onChange={(event) => onUpdate({ role: event.target.value })}
          className="h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-2.5 text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
        />
        <textarea
          value={asset.description}
          onChange={(event) => onUpdate({ description: event.target.value })}
          rows={2}
          className="min-h-16 w-full resize-none rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-2.5 text-xs leading-5 text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
        />
        <textarea
          value={asset.continuity}
          onChange={(event) => onUpdate({ continuity: event.target.value })}
          rows={2}
          className="min-h-16 w-full resize-none rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] p-2.5 text-xs leading-5 text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
        />
      </div>
      {task && (
        <div className="h-1.5 overflow-hidden rounded-full bg-[var(--bg-2)]">
          <div
            className={cn(
              "h-full rounded-full transition-[width]",
              task.status === "succeeded"
                ? "bg-[var(--success)]"
                : task.status === "failed"
                  ? "bg-[var(--danger)]"
                  : "bg-[var(--accent)]",
            )}
            style={{ width: `${progress}%` }}
          />
        </div>
      )}
      <Button
        variant={asset.imageUrl ? "outline" : "secondary"}
        size="sm"
        className="w-full"
        loading={generating}
        onClick={onGenerate}
        leftIcon={<ImageIcon className="h-3.5 w-3.5" />}
      >
        {asset.imageUrl ? "重新生成参考图" : "生成参考图"}
      </Button>
      <div className="grid grid-cols-2 gap-2">
        <Button
          variant="secondary"
          size="sm"
          disabled={!asset.imageId || asset.approved}
          onClick={onApprove}
          leftIcon={<CircleCheck className="h-3.5 w-3.5" />}
        >
          批准采用
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={!asset.approved}
          onClick={onRevokeApproval}
        >
          撤回
        </Button>
      </div>
    </article>
  );
}

function StoryboardGateMetric({
  label,
  value,
  state,
}: {
  label: string;
  value: string;
  state: "done" | "draft" | "warning";
}) {
  return (
    <div
      className={cn(
        "rounded-[var(--radius-control)] border px-2.5 py-2",
        state === "done"
          ? "border-success-border bg-success-soft text-success"
          : state === "warning"
            ? "border-warning-border bg-warning-soft text-warning"
            : "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]",
      )}
    >
      <p className="text-[10px]">{label}</p>
      <p className="mt-0.5 truncate text-sm font-semibold tabular-nums">{value}</p>
    </div>
  );
}

function StoryboardMetric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-2.5 py-2">
      <p className="text-[10px] text-[var(--fg-2)]">{label}</p>
      <p className="mt-0.5 truncate text-sm font-semibold tabular-nums text-[var(--fg-0)]">
        {value}
      </p>
    </div>
  );
}

function StoryboardShotCard({
  shot,
  index,
  total,
  selected,
  generation,
  keyframeTask,
  keyframeStale,
  boundAssetCount,
  onSelect,
  onDuplicate,
  onRemove,
  onMoveUp,
  onMoveDown,
}: {
  shot: StoryboardShot;
  index: number;
  total: number;
  selected: boolean;
  generation?: VideoGenerationOut;
  keyframeTask?: BackendGeneration;
  keyframeStale: boolean;
  boundAssetCount: number;
  onSelect: () => void;
  onDuplicate: () => void;
  onRemove: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
}) {
  const active = generation ? isActiveVideo(generation) : false;
  const succeeded = generation?.status === "succeeded" && Boolean(generation.video);
  const failed = generation ? isFailedHistoryVideo(generation) : false;
  const keyframeActive = keyframeTask ? isActiveImageTask(keyframeTask) : false;
  const keyframeFailed = keyframeTask?.status === "failed" || keyframeTask?.status === "canceled";
  const keyframeSucceeded = Boolean(shot.keyframeImageId);
  const progress = generation
    ? progressForItem(generation)
    : keyframeTask
      ? imageTaskProgress(keyframeTask)
      : 0;
  const label = succeeded
    ? "已成片"
    : failed
      ? "视频失败"
      : active
        ? "视频中"
        : keyframeStale
          ? "图过期"
          : keyframeSucceeded && shot.keyframeApproved
            ? "帧已批"
            : keyframeSucceeded
              ? "待批帧"
              : keyframeFailed
                ? "出图失败"
                : keyframeActive
                  ? "出图中"
                  : "草稿";
  return (
    <article
      className={cn(
        "group relative flex min-w-[260px] flex-col rounded-[var(--radius-card)] border p-3 text-left transition-colors lg:min-w-0",
        selected
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-0)]/62 hover:border-[var(--border)] hover:bg-[var(--bg-1)]/78",
      )}
    >
      {selected && (
        <span aria-hidden="true" className="absolute inset-y-3 left-0 w-1 rounded-r-full bg-[var(--accent)]" />
      )}
      <button type="button" onClick={onSelect} className="min-w-0 text-left">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
              SHOT {String(index + 1).padStart(2, "0")}
            </p>
            <h3 className="mt-1 line-clamp-2 text-sm font-semibold text-[var(--fg-0)]">
              {shot.title}
            </h3>
          </div>
          <span
            className={cn(
              "shrink-0 rounded-full border px-2 py-1 text-[11px]",
              succeeded
                ? "border-success-border bg-success-soft text-success"
                : failed
                  ? "border-danger-border bg-danger-soft text-danger"
                  : active
                    ? "border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]"
                    : keyframeStale
                      ? "border-warning-border bg-warning-soft text-warning"
                      : keyframeSucceeded && shot.keyframeApproved
                        ? "border-success-border bg-success-soft text-success"
                        : keyframeSucceeded
                          ? "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]"
                          : keyframeFailed
                            ? "border-danger-border bg-danger-soft text-danger"
                            : keyframeActive
                              ? "border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]"
                              : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-2)]",
            )}
          >
            {label}
          </span>
        </div>
        {shot.keyframeImageUrl && (
          <img
            src={shot.keyframeImageUrl}
            alt={shot.title}
            className="mt-2 aspect-video w-full rounded-[var(--radius-control)] border border-[var(--border)] object-cover"
          />
        )}
        <p className="mt-2 line-clamp-2 text-xs leading-5 text-[var(--fg-2)]">
          {shot.visual}
        </p>
        <div className="mt-2 flex flex-wrap gap-1.5 text-[11px] text-[var(--fg-2)]">
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-0.5">
            {shot.durationS}s
          </span>
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-0.5">
            {shot.shotType}
          </span>
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-0.5">
            {shot.cameraMove}
          </span>
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-0.5">
            {boundAssetCount} 设定
          </span>
          {shot.approved && (
            <span className="rounded-full border border-success-border bg-success-soft px-2 py-0.5 text-success">
              镜头已批
            </span>
          )}
        </div>
      </button>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <div
          className={cn(
            "h-full rounded-full transition-[width]",
            succeeded || (keyframeSucceeded && shot.keyframeApproved && !keyframeStale)
              ? "bg-[var(--success)]"
              : active || keyframeActive || keyframeStale
                ? "bg-[var(--accent)]"
                : "bg-[var(--fg-3)]",
          )}
          style={{ width: `${generation || keyframeTask ? progress : 0}%` }}
        />
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        <button
          type="button"
          onClick={onMoveUp}
          disabled={index === 0}
          aria-label="上移镜头"
          className="inline-flex h-8 w-8 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] disabled:opacity-35"
        >
          <ArrowUp className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          onClick={onMoveDown}
          disabled={index === total - 1}
          aria-label="下移镜头"
          className="inline-flex h-8 w-8 items-center justify-center rounded-[var(--radius-control)] border border-[var(--border)] text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-2)] disabled:opacity-35"
        >
          <ArrowDown className="h-3.5 w-3.5" />
        </button>
        <Button variant="outline" size="sm" onClick={onDuplicate}>
          复制
        </Button>
        <Button variant="outline" size="sm" onClick={onRemove}>
          删除
        </Button>
      </div>
    </article>
  );
}

function StoryboardShotEditor({
  shot,
  generation,
  keyframeTask,
  assets,
  selectedAssets,
  referenceImageCount,
  keyframeStale,
  prompt,
  keyframePrompt,
  canSubmit,
  submitting,
  generatingKeyframe,
  submitReason,
  onUpdate,
  onApproveShot,
  onGenerateKeyframe,
  onApproveKeyframe,
  onGenerateAllKeyframes,
  onSubmit,
  onSubmitAll,
  onUseSingleGenerator,
  onCopyPrompt,
  onPreview,
  onCancelGeneration,
  onRetryGeneration,
}: {
  shot?: StoryboardShot;
  generation?: VideoGenerationOut;
  keyframeTask?: BackendGeneration;
  assets: StoryboardReferenceAsset[];
  selectedAssets: StoryboardReferenceAsset[];
  referenceImageCount: number;
  keyframeStale: boolean;
  prompt: string;
  keyframePrompt: string;
  canSubmit: boolean;
  submitting: boolean;
  generatingKeyframe: boolean;
  submitReason: string;
  onUpdate: (id: string, patch: StoryboardShotPatch) => void;
  onApproveShot: () => void;
  onGenerateKeyframe: () => void;
  onApproveKeyframe: () => void;
  onGenerateAllKeyframes: () => void;
  onSubmit: () => void;
  onSubmitAll: () => void;
  onUseSingleGenerator: () => void;
  onCopyPrompt: () => void;
  onPreview: (item: VideoGenerationWithVideo) => void;
  onCancelGeneration: (item: VideoGenerationOut) => void;
  onRetryGeneration: (item: VideoGenerationOut) => void;
}) {
  if (!shot) {
    return (
      <div className="grid min-h-[420px] place-items-center p-6">
        <EmptyPanel
          icon={<ClipboardList className="h-5 w-5" />}
          title="还没有镜头"
          description="先从左侧脚本拆镜，或手动添加一个镜头。"
        />
      </div>
    );
  }
  const videoItem = generation && hasVideo(generation) ? generation : null;
  const keyframeProgress = imageTaskProgress(keyframeTask);
  const boundAssetIds = assetIdsForShot(shot, assets);
  const boundAssetIdSet = new Set(boundAssetIds);
  const approvedBoundCount = selectedAssets.filter(
    (asset) => asset.approved && asset.imageId,
  ).length;
  const canGenerateKeyframe =
    shot.approved &&
    (selectedAssets.length === 0 ||
      approvedBoundCount > 0 ||
      referenceImageCount > 0);
  const toggleAssetBinding = (assetId: string) => {
    const next = boundAssetIdSet.has(assetId)
      ? boundAssetIds.filter((id) => id !== assetId)
      : [...boundAssetIds, assetId];
    onUpdate(shot.id, { assetIds: next });
  };
  return (
    <div className="min-h-0 overflow-y-auto overscroll-contain p-3 sm:p-4">
      <div className="grid gap-3">
        <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_110px]">
          <label className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">镜头标题</span>
            <input
              value={shot.title}
              onChange={(event) => onUpdate(shot.id, { title: event.target.value })}
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
            />
          </label>
          <label className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">时长</span>
            <input
              value={shot.durationS}
              type="number"
              min={STORYBOARD_MIN_SHOT_DURATION_S}
              max={STORYBOARD_MAX_GROUP_DURATION_S}
              onChange={(event) =>
                onUpdate(shot.id, { durationS: Number(event.target.value) })
              }
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
            />
          </label>
        </div>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">镜头目的</span>
          <input
            value={shot.purpose}
            onChange={(event) => onUpdate(shot.id, { purpose: event.target.value })}
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
          />
        </label>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">画面内容</span>
          <textarea
            value={shot.visual}
            onChange={(event) => onUpdate(shot.id, { visual: event.target.value })}
            rows={4}
            className="min-h-28 w-full resize-none rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-3 text-sm leading-6 text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
          />
        </label>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">台词 / 旁白 / 字幕信息</span>
          <textarea
            value={shot.narration}
            onChange={(event) => onUpdate(shot.id, { narration: event.target.value })}
            rows={3}
            className="min-h-24 w-full resize-none rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-3 text-sm leading-6 text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
          />
        </label>
        <div className="grid gap-2 sm:grid-cols-3">
          <SelectField
            label="景别"
            value={shot.shotType}
            onChange={(value) => onUpdate(shot.id, { shotType: value })}
            options={STORYBOARD_SHOT_TYPES}
          />
          <SelectField
            label="运镜"
            value={shot.cameraMove}
            onChange={(value) => onUpdate(shot.id, { cameraMove: value })}
            options={STORYBOARD_CAMERA_MOVES}
          />
          <label className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">转场</span>
            <input
              value={shot.transition}
              onChange={(event) => onUpdate(shot.id, { transition: event.target.value })}
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
            />
          </label>
        </div>
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">参考素材说明</span>
          <input
            value={shot.referenceNotes}
            onChange={(event) =>
              onUpdate(shot.id, { referenceNotes: event.target.value })
            }
            placeholder="例如：参考图 1 保持人物服装，参考图 2 保持产品外观。"
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50 placeholder:text-[var(--fg-2)]"
          />
        </label>
        <section className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/58 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="type-caption text-[var(--fg-2)]">镜头设定绑定</p>
              <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                {boundAssetIds.length} 个设定 · {approvedBoundCount} 个可用于出图 · {referenceImageCount} 张侧栏参考
              </p>
            </div>
            <Button
              variant={shot.approved ? "outline" : "secondary"}
              size="sm"
              onClick={onApproveShot}
              disabled={shot.approved || boundAssetIds.length === 0}
              leftIcon={<CircleCheck className="h-3.5 w-3.5" />}
            >
              {shot.approved ? "镜头已批准" : "批准镜头"}
            </Button>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {assets.map((asset) => {
              const checked = boundAssetIdSet.has(asset.id);
              return (
                <label
                  key={asset.id}
                  className={cn(
                    "flex min-h-11 cursor-pointer items-center gap-2 rounded-[var(--radius-control)] border px-2.5 py-2 text-xs transition-colors",
                    checked
                      ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]"
                      : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-2)]",
                  )}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => toggleAssetBinding(asset.id)}
                  />
                  <span className="min-w-0 flex-1 truncate">
                    {storyboardAssetLabel(asset.kind)} · {asset.name}
                  </span>
                  <span
                    className={cn(
                      "shrink-0 rounded-full border px-1.5 py-0.5 text-[10px]",
                      asset.approved
                        ? "border-success-border bg-success-soft text-success"
                        : "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]",
                    )}
                  >
                    {asset.approved ? "已批" : "草稿"}
                  </span>
                </label>
              );
            })}
          </div>
        </section>
        <section className="space-y-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/72 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="type-caption text-[var(--fg-2)]">分镜图</p>
              <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                先批准镜头和设定，再确认画面用于视频片段
              </p>
            </div>
            {keyframeStale && (
              <span className="rounded-full border border-warning-border bg-warning-soft px-2 py-1 text-[11px] text-warning">
                当前图已过期
              </span>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={!canGenerateKeyframe}
                loading={generatingKeyframe}
                onClick={onGenerateKeyframe}
                leftIcon={<ImageIcon className="h-3.5 w-3.5" />}
              >
                {shot.keyframeImageId ? "重新生成" : "生成分镜图"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={onGenerateAllKeyframes}
                leftIcon={<ListChecks className="h-3.5 w-3.5" />}
              >
                补齐全部
              </Button>
            </div>
          </div>
          {shot.keyframeImageUrl && (
            <img
              src={shot.keyframeImageUrl}
              alt={shot.title}
              className="aspect-video w-full rounded-[var(--radius-card)] border border-[var(--border)] object-cover"
            />
          )}
          {shot.keyframeImageId && (
            <div className="flex flex-wrap gap-2">
              <Button
                variant={shot.keyframeApproved && !keyframeStale ? "outline" : "secondary"}
                size="sm"
                disabled={shot.keyframeApproved && !keyframeStale}
                onClick={onApproveKeyframe}
                leftIcon={<CircleCheck className="h-3.5 w-3.5" />}
              >
                {shot.keyframeApproved && !keyframeStale ? "关键帧已批准" : "批准关键帧"}
              </Button>
            </div>
          )}
          {keyframeTask && (
            <div className="space-y-1.5">
              <div className="flex items-center justify-between text-[11px] text-[var(--fg-2)]">
                <span>
                  {keyframeTask.status === "succeeded"
                    ? "分镜图已完成"
                    : keyframeTask.status === "failed"
                      ? "分镜图失败"
                      : "分镜图生成中"}
                </span>
                <span>{keyframeProgress}%</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-[var(--bg-2)]">
                <div
                  className={cn(
                    "h-full rounded-full transition-[width]",
                    keyframeTask.status === "succeeded"
                      ? "bg-[var(--success)]"
                      : keyframeTask.status === "failed"
                        ? "bg-[var(--danger)]"
                        : "bg-[var(--accent)]",
                  )}
                  style={{ width: `${keyframeProgress}%` }}
                />
              </div>
            </div>
          )}
          <label className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">分镜图提示词</span>
            <textarea
              value={keyframePrompt}
              onChange={(event) =>
                onUpdate(shot.id, { keyframePrompt: event.target.value })
              }
              rows={6}
              className="min-h-32 w-full resize-none rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-3 text-xs leading-5 text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
            />
          </label>
        </section>
        {videoItem && (
          <VideoPosterButton
            item={videoItem}
            onPreview={() => onPreview(videoItem)}
            compact
          />
        )}
        {generation && !videoItem && (
          <TaskRow
            item={generation}
            onCancel={() => onCancelGeneration(generation)}
            onRetry={() => onRetryGeneration(generation)}
            onCopy={onCopyPrompt}
            showPreview={false}
          />
        )}
        <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/70 p-3">
          <div className="flex items-center justify-between gap-2">
            <p className="type-caption text-[var(--fg-2)]">当前镜头提示词</p>
            <Button
              variant="ghost"
              size="sm"
              onClick={onCopyPrompt}
              leftIcon={<Copy className="h-3.5 w-3.5" />}
            >
              复制
            </Button>
          </div>
          <p className="mt-2 max-h-36 overflow-y-auto whitespace-pre-wrap text-xs leading-5 text-[var(--fg-1)]">
            {prompt}
          </p>
        </div>
        <div className="grid gap-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3 sm:grid-cols-[minmax(0,1fr)_auto_auto] sm:items-center">
          <p
            className={cn(
              "text-xs leading-5",
              canSubmit ? "text-success" : "text-[var(--fg-2)]",
            )}
          >
            {submitReason}
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={onUseSingleGenerator}
            leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
          >
            套入单条
          </Button>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={submitting}
              onClick={onSubmitAll}
              leftIcon={<ListChecks className="h-3.5 w-3.5" />}
            >
              全部提交
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={!canSubmit}
              loading={submitting}
              onClick={onSubmit}
              leftIcon={<Send className="h-3.5 w-3.5" />}
            >
              用图生成视频
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

function StoryboardSidePanel({
  action,
  selectedModel,
  modelOptions,
  resolution,
  resolutionOptions,
  aspectRatio,
  aspectRatioOptions,
  generateAudio,
  seed,
  estimate,
  submitReason,
  activeCount,
  referenceMedia,
  referenceUploading,
  assetUrlInput,
  selectedGeneration,
  onModelChange,
  onResolutionChange,
  onAspectRatioChange,
  onGenerateAudioChange,
  onSeedChange,
  onReferenceUploadClick,
  onRemoveReference,
  onInsertReference,
  onAssetUrlInputChange,
  onAddAssetReference,
  onPreview,
  onCancelGeneration,
  onRetryGeneration,
}: {
  action: VideoAction;
  selectedModel: string;
  modelOptions: string[];
  resolution: string;
  resolutionOptions: string[];
  aspectRatio: string;
  aspectRatioOptions: string[];
  generateAudio: boolean;
  seed: string;
  estimate: { tokens: number; micro: number } | null;
  submitReason: string;
  activeCount: number;
  referenceMedia: ReferenceDraft[];
  referenceUploading: boolean;
  assetUrlInput: string;
  selectedGeneration?: VideoGenerationOut;
  onModelChange: (value: string) => void;
  onResolutionChange: (value: string) => void;
  onAspectRatioChange: (value: string) => void;
  onGenerateAudioChange: (value: boolean) => void;
  onSeedChange: (value: string) => void;
  onReferenceUploadClick: () => void;
  onRemoveReference: (key: string) => void;
  onInsertReference: (label: string) => void;
  onAssetUrlInputChange: (value: string) => void;
  onAddAssetReference: () => void;
  onPreview: (item: VideoGenerationWithVideo) => void;
  onCancelGeneration: (item: VideoGenerationOut) => void;
  onRetryGeneration: (item: VideoGenerationOut) => void;
}) {
  const referenceImageCount = storyboardReferenceImageIds(referenceMedia).length;

  return (
    <aside className="min-h-0 overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/88 shadow-[var(--shadow-2)] backdrop-blur-xl">
      <div className="flex h-full min-h-0 flex-col">
        <header className="border-b border-[var(--border-subtle)] p-3">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
                <Settings2 className="h-4 w-4" />
              </span>
              <div className="min-w-0">
                <p className="type-card-title">逐段生成</p>
                <p className="mt-0.5 truncate text-xs text-[var(--fg-2)]">
                  {actionLabel(action)} · {activeCount} 个任务活跃
                </p>
              </div>
            </div>
            <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 text-xs text-[var(--fg-2)]">
              {submitReason}
            </span>
          </div>
        </header>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto overscroll-contain p-3">
          <div className="grid gap-2 sm:grid-cols-2 2xl:grid-cols-1">
            <SelectField
              label="模型"
              value={selectedModel}
              onChange={onModelChange}
              options={modelOptions}
            />
            <div className="grid grid-cols-2 gap-2">
              <SelectField
                label="分辨率"
                value={resolution}
                onChange={onResolutionChange}
                options={resolutionOptions}
              />
              <SelectField
                label="比例"
                value={aspectRatio}
                onChange={onAspectRatioChange}
                options={aspectRatioOptions}
              />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <label className="space-y-1.5">
                <span className="type-caption text-[var(--fg-2)]">Seed</span>
                <input
                  value={seed}
                  onChange={(event) => onSeedChange(event.target.value)}
                  inputMode="numeric"
                  placeholder="随机"
                  className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 font-mono text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
                />
              </label>
              <label className="flex min-h-10 items-center justify-between gap-4 self-end rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm">
                <span className="font-medium text-[var(--fg-0)]">音频</span>
                <input
                  type="checkbox"
                  checked={generateAudio}
                  onChange={(event) => onGenerateAudioChange(event.target.checked)}
                />
              </label>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72 p-3">
            <div>
              <p className="type-caption text-[var(--fg-2)]">当前镜头预扣</p>
              <p className="mt-1 text-base font-semibold tabular-nums text-[var(--fg-0)]">
                {estimate ? formatRmb(estimate.micro / 1_000_000) : "-"}
              </p>
            </div>
            <div>
              <p className="type-caption text-[var(--fg-2)]">Token 上限</p>
              <p className="mt-1 text-base font-semibold tabular-nums text-[var(--fg-0)]">
                {estimate ? estimate.tokens.toLocaleString() : "-"}
              </p>
            </div>
          </div>

          <section className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/58 p-3">
            <div className="flex items-center justify-between gap-2">
              <div>
                <p className="type-caption text-[var(--fg-2)]">角色 / 产品参考</p>
                <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                  {referenceImageCount > 0
                    ? `${referenceImageCount} 张上传图片会参与分镜图`
                    : "上传图片会参与分镜图，素材链接写入镜头说明"}
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                loading={referenceUploading}
                onClick={onReferenceUploadClick}
                leftIcon={<Upload className="h-3.5 w-3.5" />}
              >
                上传
              </Button>
            </div>
            <div className="flex flex-wrap gap-2">
              {referenceMedia.map((item) => (
                <ReferenceChip
                  key={item._key}
                  item={item}
                  onInsert={() => onInsertReference(item.label)}
                  onRemove={() => onRemoveReference(item._key)}
                />
              ))}
              {referenceMedia.length === 0 && (
                <span className="rounded-[var(--radius-control)] border border-dashed border-[var(--border)] bg-[var(--bg-1)]/70 px-3 py-2 text-xs text-[var(--fg-2)]">
                  未添加参考素材
                </span>
              )}
            </div>
            <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
              <input
                value={assetUrlInput}
                onChange={(event) => onAssetUrlInputChange(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    onAddAssetReference();
                  }
                }}
                placeholder="asset://asset-..."
                className="h-10 min-w-0 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 font-mono text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
              />
              <Button
                variant="outline"
                size="sm"
                disabled={!assetUrlInput.trim()}
                onClick={onAddAssetReference}
                leftIcon={<Tags className="h-3.5 w-3.5" />}
              >
                添加
              </Button>
            </div>
          </section>

          {selectedGeneration && (
            <section className="space-y-2">
              <p className="type-caption text-[var(--fg-2)]">当前镜头任务</p>
              <TaskRow
                item={selectedGeneration}
                onCancel={() => onCancelGeneration(selectedGeneration)}
                onRetry={() => onRetryGeneration(selectedGeneration)}
                onCopy={() => {
                  void navigator.clipboard?.writeText(selectedGeneration.prompt);
                  toast.success("描述已复制");
                }}
                onPreview={
                  hasVideo(selectedGeneration)
                    ? () => onPreview(selectedGeneration)
                    : undefined
                }
                showPreview={false}
              />
            </section>
          )}
        </div>
      </div>
    </aside>
  );
}

function SelectField({
  label,
  value,
  onChange,
  options,
  renderOption,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
  renderOption?: (value: string) => string;
}) {
  return (
    <label className="space-y-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
      >
        {options.map((item) => (
          <option key={item || "auto"} value={item}>
            {renderOption ? renderOption(item) : item || "自动"}
          </option>
        ))}
      </select>
    </label>
  );
}

function VideoParameterPanel({
  className,
  selectedModel,
  modelOptions,
  durationS,
  durationOptions,
  resolution,
  resolutionOptions,
  aspectRatio,
  aspectRatioOptions,
  seed,
  generateAudio,
  estimate,
  canSubmit,
  reason,
  loading,
  sourceReady,
  onSubmit,
  onModelChange,
  onDurationChange,
  onResolutionChange,
  onAspectRatioChange,
  onSeedChange,
  onGenerateAudioChange,
}: {
  className?: string;
  selectedModel: string;
  modelOptions: string[];
  durationS: number;
  durationOptions: string[];
  resolution: string;
  resolutionOptions: string[];
  aspectRatio: string;
  aspectRatioOptions: string[];
  seed: string;
  generateAudio: boolean;
  estimate: { tokens: number; micro: number } | null;
  canSubmit: boolean;
  reason: string;
  loading: boolean;
  sourceReady: boolean;
  onSubmit: () => void;
  onModelChange: (value: string) => void;
  onDurationChange: (value: string) => void;
  onResolutionChange: (value: string) => void;
  onAspectRatioChange: (value: string) => void;
  onSeedChange: (value: string) => void;
  onGenerateAudioChange: (value: boolean) => void;
}) {
  return (
    <aside
      className={cn(
        "space-y-3 rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/88 p-3 shadow-[var(--shadow-2)] backdrop-blur-xl",
        className,
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--accent)]">
              <Settings2 className="h-4 w-4" />
            </span>
            <div className="min-w-0">
              <p className="text-sm font-semibold text-[var(--fg-0)]">生成参数</p>
              <p className="mt-0.5 truncate text-xs text-[var(--fg-2)]">
                {selectedModel || "未选择模型"}
              </p>
            </div>
          </div>
        </div>
        <span
          className={cn(
            "rounded-full border px-2 py-1 text-xs",
            canSubmit
              ? "border-success-border bg-success-soft text-success"
              : sourceReady
                ? "border-[var(--border)] bg-[var(--bg-0)] text-[var(--fg-2)]"
                : "border-warning-border bg-warning-soft text-[var(--warning-fg)]",
          )}
        >
          {canSubmit ? "就绪" : sourceReady ? "草稿" : "缺素材"}
        </span>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
        <SelectField
          label="模型"
          value={selectedModel}
          onChange={onModelChange}
          options={modelOptions}
        />
        <div className="grid grid-cols-2 gap-2">
          <SelectField
            label="分辨率"
            value={resolution}
            onChange={onResolutionChange}
            options={resolutionOptions}
          />
          <SelectField
            label="比例"
            value={aspectRatio}
            onChange={onAspectRatioChange}
            options={aspectRatioOptions}
          />
        </div>
        <div className="grid grid-cols-2 gap-2">
          <SelectField
            label="时长"
            value={String(durationS)}
            onChange={onDurationChange}
            options={durationOptions}
            renderOption={(value) => formatDurationLabel(Number(value))}
          />
          <label className="space-y-1.5">
            <span className="type-caption text-[var(--fg-2)]">Seed</span>
            <input
              value={seed}
              onChange={(event) => onSeedChange(event.target.value)}
              inputMode="numeric"
              placeholder="随机"
              className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 font-mono text-xs text-[var(--fg-0)] outline-none focus:border-[var(--accent)]/50"
            />
          </label>
        </div>
        <label className="flex min-h-10 items-center justify-between gap-4 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm">
          <span className="font-medium text-[var(--fg-0)]">生成音频</span>
          <input
            type="checkbox"
            checked={generateAudio}
            onChange={(event) => onGenerateAudioChange(event.target.checked)}
          />
        </label>
      </div>

      <div className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72 p-3">
        <div className="grid grid-cols-2 gap-2">
          <div>
            <p className="type-caption text-[var(--fg-2)]">预扣</p>
            <p className="mt-1 text-base font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? formatRmb(estimate.micro / 1_000_000) : "-"}
            </p>
          </div>
          <div>
            <p className="type-caption text-[var(--fg-2)]">Token 上限</p>
            <p className="mt-1 text-base font-semibold tabular-nums text-[var(--fg-0)]">
              {estimate ? estimate.tokens.toLocaleString() : "-"}
            </p>
          </div>
        </div>
      </div>

      <SubmitPanel
        canSubmit={canSubmit}
        reason={reason}
        loading={loading}
        onSubmit={onSubmit}
        compact
      />
    </aside>
  );
}

function VideoWorkbenchHeader({
  mode,
  profile,
  audio,
  enabled,
  loading,
  activeCount,
  completedCount,
  serviceSummary,
  submitState,
}: {
  mode: string;
  profile: string;
  audio: boolean;
  enabled: boolean;
  loading: boolean;
  activeCount: number;
  completedCount: number;
  serviceSummary: string;
  submitState: string;
}) {
  const serviceValue = loading ? "读取中" : enabled ? "在线" : "离线";
  const serviceDetail = loading ? "读取配置" : serviceSummary;
  const queueValue = activeCount > 0 ? `${activeCount} 进行中` : `${completedCount} 已完成`;
  const queueDetail = activeCount > 0 ? "任务队列" : "最近结果";

  return (
    <section className="grid shrink-0 gap-2 border-b border-[var(--border)] pb-2 lg:grid-cols-[minmax(0,1fr)_minmax(520px,0.86fr)] lg:items-center">
      <div className="min-w-0">
        <div className="hidden max-w-full items-center gap-2 rounded-full border border-[var(--border)] bg-[var(--bg-1)]/72 px-2.5 py-1 text-xs font-medium text-[var(--fg-1)] shadow-[var(--shadow-1)] sm:inline-flex">
          <Sparkles className="h-3.5 w-3.5 shrink-0 text-[var(--accent)]" />
          <span className="truncate">Lumen 视频工作台</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 sm:mt-1.5">
          <h1 className="text-2xl font-semibold leading-tight tracking-normal text-[var(--fg-0)] sm:type-page-title-sm">
            视频工作台
          </h1>
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)]/72 px-2 py-0.5 text-xs text-[var(--fg-2)]">
            {submitState}
          </span>
        </div>
      </div>
      <div className="hidden min-w-0 grid-cols-[repeat(3,minmax(0,1fr))] gap-1.5 sm:grid sm:gap-2">
        <StatusStripItem
          label="服务"
          value={serviceValue}
          detail={serviceDetail}
          icon={<Clapperboard className="h-3.5 w-3.5" />}
          active={enabled}
        />
        <StatusStripItem
          label="模式"
          value={mode}
          detail={audio ? "含音频" : "无音频"}
          icon={<Film className="h-3.5 w-3.5" />}
          active
        />
        <StatusStripItem
          label="规格"
          value={profile}
          detail={`${queueValue} · ${queueDetail}`}
          icon={<Gauge className="h-3.5 w-3.5" />}
          active={activeCount > 0}
        />
      </div>
    </section>
  );
}

function StatusStripItem({
  label,
  value,
  detail,
  icon,
  active = false,
}: {
  label: string;
  value: string;
  detail: string;
  icon: React.ReactNode;
  active?: boolean;
}) {
  return (
    <div
      className={cn(
        "relative min-w-0 overflow-hidden rounded-[var(--radius-control)] border px-2 py-1.5 sm:px-2.5 sm:py-2",
        active
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-0)]/64",
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "absolute inset-y-2 left-0 w-0.5 rounded-r-full",
          active ? "bg-[var(--accent)]" : "bg-[var(--border-strong)]",
        )}
      />
      <div className="flex min-w-0 items-start gap-1.5 sm:gap-2.5">
        <span
          className={cn(
            "mt-0.5 hidden h-6 w-6 shrink-0 items-center justify-center rounded-[var(--radius-control)] border sm:flex",
            active
              ? "border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]"
              : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]",
          )}
        >
          {icon}
        </span>
        <span className="min-w-0">
          <span className="block truncate text-[11px] leading-tight text-[var(--fg-2)] sm:type-caption">
            {label}
          </span>
          <span className="mt-0.5 block truncate text-[10px] font-semibold text-[var(--fg-0)] sm:text-xs">
            {value}
          </span>
          <span className="mt-0.5 hidden truncate text-[11px] text-[var(--fg-2)] sm:block">
            {detail}
          </span>
        </span>
      </div>
    </div>
  );
}

function ModeCard({
  actionKey,
  selected,
  onSelect,
}: {
  actionKey: VideoAction;
  selected: boolean;
  onSelect: () => void;
}) {
  const copy = MODE_COPY[actionKey];
  const icon =
    actionKey === "t2v" ? (
      <Film className="h-4 w-4" />
    ) : actionKey === "i2v" ? (
      <ImageIcon className="h-4 w-4" />
    ) : (
      <VideoIcon className="h-4 w-4" />
    );
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "group relative min-h-[54px] min-w-0 overflow-hidden rounded-[var(--radius-control)] border px-2.5 py-2 text-left transition-[background-color,border-color,color,transform] duration-200",
        selected
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
          : "border-transparent text-[var(--fg-1)] hover:border-[var(--border)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "absolute inset-x-2 bottom-0 h-0.5 rounded-t-full transition-colors",
          selected ? "bg-[var(--accent)]" : "bg-transparent",
        )}
      />
      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            "flex h-7 w-7 shrink-0 items-center justify-center rounded-[var(--radius-control)] border",
            selected
              ? "border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]"
              : "border-[var(--border-subtle)] bg-[var(--bg-1)] text-[var(--fg-2)]",
          )}
        >
          {icon}
        </span>
        <span
          className={cn(
            "mt-0.5 h-2 w-2 shrink-0 rounded-full",
            selected ? "bg-[var(--accent)]" : "bg-[var(--fg-3)]",
          )}
        />
      </div>
      <p className="mt-1.5 text-sm font-semibold text-[var(--fg-0)]">
        {copy.title}
      </p>
      <p className="mt-0.5 truncate text-[11px] font-medium text-[var(--fg-2)]">
        {copy.eyebrow}
      </p>
    </button>
  );
}

function PromptEnhanceChooser({
  loading,
  preview,
  candidates,
  selectedId,
  onSelect,
  onDismiss,
}: {
  loading: boolean;
  preview: string;
  candidates: PromptEnhanceCandidate[];
  selectedId: string;
  onSelect: (candidate: PromptEnhanceCandidate) => void;
  onDismiss: () => void;
}) {
  const cleanPreview = cleanPromptEnhanceText(preview);
  const visibleCandidates = candidates.length > 0 ? candidates : [];

  const copyCandidate = async (candidate: PromptEnhanceCandidate) => {
    try {
      await navigator.clipboard.writeText(candidate.prompt);
      toast.success("已复制提示词");
    } catch {
      toast.error("复制失败");
    }
  };

  return (
    <div className="space-y-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)]/78 p-3 shadow-[var(--shadow-1)]">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] border border-[var(--accent-border)] bg-[var(--bg-0)] text-[var(--accent)]">
            {loading ? (
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Sparkles className="h-3.5 w-3.5" />
            )}
          </span>
          <span className="min-w-0">
            <span className="block text-sm font-semibold text-[var(--fg-0)]">
              {loading ? "正在优化提示词" : "优化方案"}
            </span>
            <span className="block truncate text-xs text-[var(--fg-2)]">
              {visibleCandidates.length > 1
                ? `${visibleCandidates.length} 个候选，已应用推荐版`
                : loading
                  ? "优先补运动、运镜和时间推进"
                  : "已应用到描述"}
            </span>
          </span>
        </div>
        {!loading && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onDismiss}
            leftIcon={<XCircle className="h-3.5 w-3.5" />}
          >
            清除
          </Button>
        )}
      </div>

      {loading && (
        <div className="min-h-20 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/72 p-3 text-sm leading-6 text-[var(--fg-1)]">
          {cleanPreview || "等待上游返回..."}
        </div>
      )}

      {visibleCandidates.length > 0 && (
        <div className="grid gap-2">
          {visibleCandidates.map((candidate) => {
            const selected = candidate.id === selectedId;
            return (
              <div
                key={candidate.id}
                className={cn(
                  "rounded-[var(--radius-control)] border bg-[var(--bg-0)] p-3 transition-colors",
                  selected
                    ? "border-[var(--accent-border)] shadow-[var(--shadow-1)]"
                    : "border-[var(--border-subtle)]",
                )}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      <span
                        className={cn(
                          "flex h-5 w-5 shrink-0 items-center justify-center rounded-full border",
                          selected
                            ? "border-[var(--accent-border)] text-[var(--accent)]"
                            : "border-[var(--border)] text-[var(--fg-2)]",
                        )}
                      >
                        {selected ? (
                          <CircleCheck className="h-3.5 w-3.5" />
                        ) : (
                          <PencilLine className="h-3 w-3" />
                        )}
                      </span>
                      <p className="truncate text-sm font-semibold text-[var(--fg-0)]">
                        {candidate.title}
                      </p>
                    </div>
                    <p className="mt-2 max-h-28 overflow-y-auto text-sm leading-6 text-[var(--fg-1)]">
                      {candidate.prompt}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button
                      variant={selected ? "secondary" : "outline"}
                      size="sm"
                      disabled={selected}
                      onClick={() => onSelect(candidate)}
                    >
                      {selected ? "已用" : "使用"}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-9 w-9 px-0"
                      onClick={() => void copyCandidate(candidate)}
                      aria-label="复制优化提示词"
                    >
                      <Copy className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ReferenceChip({
  item,
  onInsert,
  onRemove,
}: {
  item: ReferenceDraft;
  onInsert: () => void;
  onRemove: () => void;
}) {
  return (
    <div className="inline-flex min-h-10 max-w-full items-center gap-2 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-2 text-xs text-[var(--fg-1)]">
      <button
        type="button"
        onClick={onInsert}
        className="inline-flex min-w-0 items-center gap-2 rounded-[var(--radius-control)] px-1 py-1 text-left transition-colors hover:bg-[var(--bg-2)]"
      >
        {item.kind === "image" ? (
          <ImageIcon className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <VideoIcon className="h-3.5 w-3.5 shrink-0" />
        )}
        <span className="shrink-0">[{item.label}]</span>
        <span className="truncate text-[var(--fg-2)]">{item.display}</span>
      </button>
      <button
        type="button"
        aria-label="移除参考素材"
        onClick={onRemove}
        className="shrink-0 rounded-full p-0.5 text-[var(--fg-2)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)]"
      >
        <XCircle className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function SubmitPanel({
  canSubmit,
  reason,
  loading,
  onSubmit,
  compact = false,
}: {
  canSubmit: boolean;
  reason: string;
  loading: boolean;
  onSubmit: () => void;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        "rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-1)]/95 shadow-[var(--shadow-2)] backdrop-blur-xl",
        compact ? "p-2.5" : "p-3",
      )}
    >
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
        <p
          className={cn(
            "min-w-0 flex-1 text-xs leading-5",
            canSubmit ? "text-success" : "text-[var(--fg-2)]",
          )}
        >
          {reason}
        </p>
        <Button
          variant="primary"
          size={compact ? "sm" : "md"}
          disabled={!canSubmit}
          loading={loading}
          onClick={onSubmit}
          leftIcon={<Send className="h-4 w-4" />}
          className="w-full sm:w-auto"
        >
          提交
        </Button>
      </div>
    </div>
  );
}

function EmptyPanel({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex min-h-[132px] flex-col items-center justify-center rounded-[var(--radius-card)] border border-dashed border-[var(--border)] bg-[var(--bg-0)]/60 p-6 text-center">
      <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-2)]">
        {icon}
      </div>
      <p className="text-sm font-medium text-[var(--fg-0)]">{title}</p>
      <p className="mt-1 max-w-sm text-xs leading-5 text-[var(--fg-2)]">{description}</p>
    </div>
  );
}

function VideoDownloadLink({
  item,
  fullWidth = false,
}: {
  item: VideoGenerationWithVideo;
  fullWidth?: boolean;
}) {
  return (
    <a
      href={videoDownloadSrc(item.video.id)}
      download={videoDownloadName(item)}
      className={cn(
        "inline-flex h-9 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border border-[var(--border)] bg-transparent px-3 text-xs font-medium leading-tight text-[var(--fg-0)] transition-[background-color,border-color,color] hover:border-[var(--border-strong)] hover:bg-[var(--bg-2)]",
        fullWidth && "w-full",
      )}
    >
      <Download className="h-3.5 w-3.5 shrink-0" />
      下载
    </a>
  );
}

function VideoPosterButton({
  item,
  onPreview,
  selected = false,
  compact = false,
}: {
  item: VideoGenerationWithVideo;
  onPreview: () => void;
  selected?: boolean;
  compact?: boolean;
}) {
  const [posterFailure, setPosterFailure] = useState<{
    videoId: string;
    failed: boolean;
  } | null>(null);
  const poster = posterSrc(item.video);
  const videoUrl = videoSrc(item.video);
  const posterFailed =
    posterFailure?.videoId === item.video.id ? posterFailure.failed : false;
  const prewarmPreview = useCallback(() => {
    prewarmImage(poster);
    prewarmVideoMetadata(videoUrl);
  }, [poster, videoUrl]);
  const handlePreview = useCallback(() => {
    prewarmPreview();
    onPreview();
  }, [onPreview, prewarmPreview]);

  useEffect(() => {
    if (selected) prewarmPreview();
  }, [prewarmPreview, selected]);

  return (
    <button
      type="button"
      onClick={handlePreview}
      onFocus={prewarmPreview}
      onPointerDown={prewarmPreview}
      onPointerEnter={prewarmPreview}
      aria-pressed={selected}
      className={cn(
        "group relative w-full overflow-hidden rounded-[var(--radius-control)] border bg-[var(--bg-0)] text-left transition-colors",
        compact ? "aspect-video" : "mt-3 aspect-video",
        selected
          ? "border-[var(--accent-border)] shadow-[var(--shadow-1)]"
          : "border-[var(--border-subtle)] hover:border-[var(--border)]",
      )}
    >
      {poster && !posterFailed ? (
        <img
          src={poster}
          alt=""
          loading={selected ? "eager" : "lazy"}
          decoding="async"
          fetchPriority={selected ? "high" : "low"}
          onError={() =>
            setPosterFailure({ videoId: item.video.id, failed: true })
          }
          className="h-full w-full object-contain"
        />
      ) : (
        <div className="grid h-full place-items-center text-[var(--fg-2)]">
          <Film className="h-6 w-6" />
        </div>
      )}
      <span className="absolute inset-0 flex items-center justify-center bg-black/0 transition-colors group-hover:bg-black/20">
        <span className="inline-flex items-center gap-1.5 rounded-full border border-[var(--border)] bg-[var(--fg-0)]/85 px-3 py-1.5 text-xs font-medium text-[var(--bg-0)] shadow-[var(--shadow-2)]">
          <Play className="h-3.5 w-3.5" />
          播放预览
        </span>
      </span>
    </button>
  );
}

type VideoPlayerStatus = "loading" | "metadata" | "ready" | "buffering" | "error";

function videoPlayerStatusLabel(status: VideoPlayerStatus): string {
  switch (status) {
    case "loading":
      return "读取视频";
    case "metadata":
      return "准备播放";
    case "buffering":
      return "缓冲中";
    case "error":
      return "载入失败";
    default:
      return "";
  }
}

function PrimaryVideoPlayer({
  item,
  className,
}: {
  item: VideoGenerationWithVideo;
  className?: string;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [statusState, setStatusState] = useState<{
    videoId: string;
    status: VideoPlayerStatus;
  }>(() => ({ videoId: item.video.id, status: "loading" }));
  const poster = posterSrc(item.video);
  const src = videoSrc(item.video);
  const status =
    statusState.videoId === item.video.id ? statusState.status : "loading";
  const setVideoStatus = useCallback(
    (next: VideoPlayerStatus) =>
      setStatusState({ videoId: item.video.id, status: next }),
    [item.video.id],
  );

  useEffect(() => {
    prewarmImage(poster);
    prewarmVideoMetadata(src);
  }, [poster, src]);

  const retryLoad = useCallback(() => {
    setVideoStatus("loading");
    prewarmImage(poster);
    prewarmVideoMetadata(src);
    videoRef.current?.load();
  }, [poster, setVideoStatus, src]);

  const showState =
    status === "loading" || status === "buffering" || status === "error";

  return (
    <div
      className={cn(
        "relative flex min-h-0 overflow-hidden rounded-[var(--radius-panel)] border border-[var(--border-strong)] bg-[var(--bg-2)] shadow-[var(--shadow-2)]",
        className,
      )}
    >
      <video
        key={item.video.id}
        ref={videoRef}
        controls
        playsInline
        preload="metadata"
        poster={poster}
        src={src}
        onLoadStart={() => setVideoStatus("loading")}
        onLoadedMetadata={() => setVideoStatus("metadata")}
        onCanPlay={() => setVideoStatus("ready")}
        onPlaying={() => setVideoStatus("ready")}
        onWaiting={() => setVideoStatus("buffering")}
        onError={() => setVideoStatus("error")}
        className="h-full min-h-0 w-full bg-[var(--bg-2)] object-contain"
      />
      {showState && (
        <div
          className={cn(
            "absolute inset-0 flex items-center justify-center bg-[var(--bg-1)]/70 text-[var(--fg-0)]",
            status !== "error" && "pointer-events-none",
          )}
        >
          <div
            role={status === "error" ? "alert" : "status"}
            aria-live={status === "error" ? "assertive" : "polite"}
            className="inline-flex items-center gap-2 rounded-full border border-[var(--border-strong)] bg-[var(--bg-0)]/90 px-3 py-1.5 text-xs font-medium text-[var(--fg-0)] shadow-[var(--shadow-2)] backdrop-blur-md"
          >
            {status === "error" ? (
              <button
                type="button"
                onClick={retryLoad}
                className="inline-flex cursor-pointer items-center gap-1.5 text-[var(--fg-0)]"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                重试
              </button>
            ) : (
              <>
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                {videoPlayerStatusLabel(status)}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function VideoPreviewDialog({
  item,
  onClose,
  onUseDraft,
  onRetry,
  onCopy,
  onDelete,
}: {
  item: VideoGenerationWithVideo;
  onClose: () => void;
  onUseDraft: () => void;
  onRetry: () => void;
  onCopy: () => void;
  onDelete: () => void;
}) {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div
      className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center bg-black/70 backdrop-blur-md sm:items-center sm:p-5"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby={`video-preview-${item.id}`}
        className="mobile-dialog-panel flex h-[var(--mobile-dialog-max-height)] w-full max-w-6xl flex-col overflow-hidden rounded-t-[var(--radius-panel)] border border-b-0 border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-0)] shadow-[var(--shadow-3)] sm:h-[min(900px,calc(100dvh-2.5rem))] sm:rounded-[var(--radius-panel)] sm:border-b"
      >
        <header className="flex shrink-0 items-start justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-1)]/95 px-4 py-3 sm:px-5">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap gap-2">
              <StatusPill item={item} />
              <span className="rounded-full border border-[var(--border)] bg-[var(--bg-0)] px-2 py-1 text-xs text-[var(--fg-2)]">
                {actionLabel(item.action)} · {item.resolution} · {formatDurationLabel(item.duration_s)}
              </span>
            </div>
            <h2
              id={`video-preview-${item.id}`}
              className="truncate text-base font-semibold text-[var(--fg-0)]"
            >
              视频播放
            </h2>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-9 w-9 px-0"
            onClick={onClose}
            aria-label="关闭视频播放"
          >
            <XCircle className="h-4 w-4" />
          </Button>
        </header>
        <div className="min-h-0 flex-1 overflow-hidden p-3 sm:p-5">
          <div className="flex h-full min-h-0 flex-col gap-3 lg:grid lg:grid-cols-[minmax(0,1fr)_minmax(280px,340px)]">
            <div className="min-h-0 flex-1 lg:h-full">
              <PrimaryVideoPlayer item={item} className="h-full" />
            </div>
            <aside className="max-h-[34%] shrink-0 overflow-y-auto rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/64 p-3 shadow-[var(--shadow-1)] lg:h-full lg:max-h-none">
              <p className="type-caption text-[var(--fg-2)]">提示词</p>
              <p className="mt-2 text-sm leading-6 text-[var(--fg-0)]">
                {item.prompt}
              </p>
              <div className="mt-3 flex flex-wrap gap-1.5 text-xs text-[var(--fg-2)]">
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                  {item.video.width}x{item.video.height}
                </span>
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                  {formatDurationLabel(item.duration_s)}
                </span>
                <span className="rounded-full border border-[var(--border)] bg-[var(--bg-1)] px-2 py-1">
                  {item.video.has_audio ? "含音频" : "无音频"}
                </span>
              </div>
            </aside>
          </div>
        </div>
        <footer className="mobile-dialog-footer flex shrink-0 flex-nowrap items-center gap-2 overflow-x-auto border-t border-[var(--border)] bg-[var(--bg-1)]/88 px-4 py-3 sm:flex-wrap sm:justify-between sm:overflow-visible sm:px-5">
          <VideoDownloadLink item={item} />
          <div className="flex shrink-0 flex-nowrap items-center gap-2 sm:flex-wrap">
            <Button
              variant="secondary"
              size="sm"
              onClick={onUseDraft}
              leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
            >
              套用参数
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onRetry}
              leftIcon={<Play className="h-3.5 w-3.5" />}
            >
              重新生成
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onCopy}
              leftIcon={<Copy className="h-3.5 w-3.5" />}
            >
              复制
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={onDelete}
              leftIcon={<Trash2 className="h-3.5 w-3.5" />}
            >
              删除
            </Button>
          </div>
        </footer>
      </section>
    </div>
  );
}

function HistoryFilterTabs({
  value,
  counts,
  loading,
  onChange,
}: {
  value: VideoHistoryFilter;
  counts: Record<VideoHistoryFilter, number>;
  loading: boolean;
  onChange: (value: VideoHistoryFilter) => void;
}) {
  const filters: Array<{ value: VideoHistoryFilter; label: string }> = [
    { value: "all", label: "全部" },
    { value: "succeeded", label: "成功" },
    { value: "failed", label: "失败" },
  ];

  return (
    <div className="grid grid-cols-3 gap-1 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)] p-1">
      {filters.map((filter) => {
        const active = filter.value === value;
        return (
          <button
            key={filter.value}
            type="button"
            onClick={() => onChange(filter.value)}
            className={cn(
              "min-h-8 rounded-[var(--radius-control)] px-2 text-xs transition-colors",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]",
              active
                ? "bg-[var(--bg-2)] text-[var(--fg-0)] shadow-[var(--shadow-1)]"
                : "text-[var(--fg-2)] hover:bg-[var(--bg-1)] hover:text-[var(--fg-1)]",
            )}
          >
            <span className="inline-flex min-w-0 items-center justify-center gap-1.5">
              <span>{filter.label}</span>
              <span className="rounded-full border border-[var(--border)] px-1.5 py-0.5 font-mono text-[10px] tabular-nums">
                {loading ? "..." : counts[filter.value]}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function TaskRow({
  item,
  onCancel,
  onRetry,
  onCopy,
  onUseDraft,
  onDelete,
  onPreview,
  selected = false,
  showPreview = true,
}: {
  item: VideoGenerationOut;
  onCancel: () => void;
  onRetry: () => void;
  onCopy: () => void;
  onUseDraft?: () => void;
  onDelete?: () => void;
  onPreview?: () => void;
  selected?: boolean;
  showPreview?: boolean;
}) {
  const active = isActiveVideo(item);
  const progress = progressForItem(item);
  const copy = stageCopy(item);
  const videoItem = hasVideo(item) ? item : null;
  return (
    <article
      className={cn(
        "relative overflow-hidden rounded-[var(--radius-card)] border p-3 transition-colors hover:border-[var(--border)]",
        active || selected
          ? "border-[var(--accent-border)] bg-[var(--accent-soft)] shadow-[var(--shadow-1)]"
          : "border-[var(--border-subtle)] bg-[var(--bg-0)]/60",
      )}
    >
      {(active || selected) && (
        <span aria-hidden="true" className="absolute inset-y-3 left-0 w-1 rounded-r-full bg-[var(--accent)]" />
      )}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--fg-2)]">
            <span className="font-medium text-[var(--fg-1)]">{item.model}</span>
            <span>{actionLabel(item.action)}</span>
            <span>{item.resolution}</span>
            <span>{formatDurationLabel(item.duration_s)}</span>
          </div>
          <p className="mt-1 line-clamp-2 text-sm text-[var(--fg-0)]">{item.prompt}</p>
          <p className="mt-1 text-xs leading-5 text-[var(--fg-2)]">{copy.detail}</p>
        </div>
        <StatusPill item={item} />
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-[var(--bg-2)]">
        <motion.div
          className={cn(
            "h-full rounded-full",
            active ? "bg-[var(--accent)]" : item.status === "succeeded" ? "bg-[var(--success)]" : "bg-[var(--fg-3)]",
          )}
          initial={false}
          animate={{ width: `${progress}%` }}
          transition={{ duration: 0.26, ease: [0.2, 0.8, 0.2, 1] }}
        />
      </div>
      {showPreview && videoItem && onPreview && (
        <VideoPosterButton
          item={videoItem}
          selected={selected}
          onPreview={onPreview}
        />
      )}
      {item.error_message && (
        <p className="mt-2 text-xs text-[var(--danger-fg)]">{item.error_message}</p>
      )}
      <div className="mt-3 flex flex-wrap gap-2">
        {active && (
          <Button
            variant="outline"
            size="sm"
            onClick={onCancel}
            leftIcon={<XCircle className="h-3.5 w-3.5" />}
          >
            取消
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          onClick={onRetry}
          leftIcon={<Play className="h-3.5 w-3.5" />}
        >
          重新生成
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={onCopy}
          leftIcon={<Copy className="h-3.5 w-3.5" />}
        >
          复制
        </Button>
        {!showPreview && videoItem && onPreview && (
          <Button
            variant={selected ? "secondary" : "outline"}
            size="sm"
            onClick={onPreview}
            leftIcon={<Play className="h-3.5 w-3.5" />}
          >
            预览
          </Button>
        )}
        {videoItem && <VideoDownloadLink item={videoItem} />}
        {onUseDraft && (
          <Button
            variant="outline"
            size="sm"
            onClick={onUseDraft}
            leftIcon={<RotateCcw className="h-3.5 w-3.5" />}
          >
            套用参数
          </Button>
        )}
        {onDelete && videoItem && (
          <Button
            variant="outline"
            size="sm"
            onClick={onDelete}
            leftIcon={<Trash2 className="h-3.5 w-3.5" />}
          >
            删除
          </Button>
        )}
      </div>
    </article>
  );
}

function StatusPill({ item }: { item: VideoGenerationOut }) {
  const terminalOk = item.status === "succeeded";
  const terminalBad = ["failed", "canceled", "expired"].includes(item.status);
  const copy = stageCopy(item);
  return (
    <span
      className={[
        "rounded-full border px-2 py-1 text-xs",
        terminalOk
          ? "border-success-border bg-success-soft text-success"
          : terminalBad
          ? "border-danger-border bg-danger-soft text-danger"
          : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-1)]",
      ].join(" ")}
    >
      {copy.label} · {Math.round(progressForItem(item))}%
    </span>
  );
}
