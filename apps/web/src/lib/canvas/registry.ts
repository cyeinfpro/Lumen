import {
  FileImage,
  FileText,
  Film,
  Frame,
  ImagePlus,
  MessageSquareText,
  PackageCheck,
  Video,
  type LucideIcon,
} from "lucide-react";

import type {
  CanvasDataType,
  CanvasEdgeRole,
  CanvasNodeUI,
  CanvasNodeDefinition,
  CanvasNodeType,
} from "#canvas-types";

export interface CanvasPortSpec {
  id: string;
  label: string;
  dataType: CanvasDataType;
  multiple?: boolean;
  maximum?: number;
  required?: boolean;
  accepts?: CanvasDataType[];
}

export interface CanvasNodeSpec {
  type: CanvasNodeType;
  label: string;
  description: string;
  category: CanvasNodeCategory;
  executable: boolean;
  family: CanvasNodeFamily;
  keywords: readonly string[];
  icon: LucideIcon;
  width: number;
  inputs: CanvasPortSpec[];
  outputs: CanvasPortSpec[];
  defaultConfig: Record<string, unknown>;
  fixedVideoMode?: CanvasVideoMode;
}

export type CanvasNodeCategory =
  | "input"
  | "text"
  | "image"
  | "video"
  | "organize"
  | "deliver";
export type CanvasNodeFamily =
  | "text"
  | "asset"
  | "image"
  | "video"
  | "organize"
  | "delivery";
export type CanvasVideoMode = "t2v" | "i2v" | "reference";

export type CanvasNodeCreateOverrides = Partial<
  Omit<CanvasNodeDefinition, "type" | "position">
>;

export interface CanvasNodeCatalogItem {
  id: string;
  type: CanvasNodeType;
  label: string;
  description: string;
  category: CanvasNodeCategory;
  keywords: readonly string[];
  overrides?: CanvasNodeCreateOverrides;
}

export const CANVAS_NODE_SPECS: Record<CanvasNodeType, CanvasNodeSpec> = {
  prompt: {
    type: "prompt",
    label: "提示词",
    description: "文本输入",
    category: "text",
    executable: false,
    family: "text",
    keywords: ["prompt", "提示词", "文本", "描述"],
    icon: MessageSquareText,
    width: 260,
    inputs: [],
    outputs: [{ id: "text", label: "文本", dataType: "text" }],
    defaultConfig: { text: "", locked: false },
  },
  prompt_merge: {
    type: "prompt_merge",
    label: "提示词合并",
    description: "组合多个文本输入",
    category: "text",
    executable: false,
    family: "text",
    keywords: ["prompt merge", "合并", "文本", "提示词", "拼接"],
    icon: MessageSquareText,
    width: 280,
    inputs: [{ id: "texts", label: "文本", dataType: "text", multiple: true }],
    outputs: [{ id: "text", label: "文本", dataType: "text" }],
    defaultConfig: {
      separator: "\n\n",
      prefix: "",
      suffix: "",
      trim: true,
      dedupe: false,
    },
  },
  image_asset: {
    type: "image_asset",
    label: "图片素材",
    description: "上传或资产图片",
    category: "input",
    executable: false,
    family: "asset",
    keywords: ["image", "图片", "素材", "上传", "参考"],
    icon: FileImage,
    width: 248,
    inputs: [],
    outputs: [{ id: "image", label: "图片", dataType: "image" }],
    defaultConfig: { image_id: "", display_name: "", crop: null },
  },
  mask_asset: {
    type: "mask_asset",
    label: "遮罩素材",
    description: "用于局部编辑的遮罩",
    category: "input",
    executable: false,
    family: "asset",
    keywords: ["mask", "遮罩", "蒙版", "局部重绘"],
    icon: FileImage,
    width: 248,
    inputs: [],
    outputs: [{ id: "mask", label: "遮罩", dataType: "mask" }],
    defaultConfig: { image_id: "", display_name: "", crop: null },
  },
  video_asset: {
    type: "video_asset",
    label: "视频素材",
    description: "已有视频",
    category: "input",
    executable: false,
    family: "asset",
    keywords: ["video", "视频", "素材", "上传", "参考"],
    icon: Film,
    width: 272,
    inputs: [],
    outputs: [{ id: "video", label: "视频", dataType: "video" }],
    defaultConfig: { video_id: "", display_name: "" },
  },
  image_generate: {
    type: "image_generate",
    label: "图片生成",
    description: "文生图与图生图",
    category: "image",
    executable: true,
    family: "image",
    keywords: ["image generate", "文生图", "图生图", "图片", "生成"],
    icon: ImagePlus,
    width: 292,
    inputs: [
      { id: "prompt", label: "提示词", dataType: "text", required: true },
      {
        id: "references",
        label: "参考图",
        dataType: "image",
        multiple: true,
        maximum: 16,
      },
      {
        id: "mask",
        label: "遮罩",
        dataType: "mask",
        accepts: ["image", "mask"],
      },
    ],
    outputs: [{ id: "image", label: "图片", dataType: "image" }],
    defaultConfig: {
      model: null,
      aspect_ratio: "1:1",
      size: "1K",
      quality: "2k",
      size_mode: "auto",
      fixed_size: null,
      render_quality: "high",
      count: 1,
      fast: true,
      output_format: "webp",
      output_compression: null,
      background: "auto",
      moderation: "low",
    },
  },
  image_edit: {
    type: "image_edit",
    label: "图片编辑",
    description: "基于原图和提示词编辑",
    category: "image",
    executable: true,
    family: "image",
    keywords: ["image edit", "图片编辑", "重绘", "编辑", "透明背景"],
    icon: ImagePlus,
    width: 300,
    inputs: [
      { id: "prompt", label: "提示词", dataType: "text", required: true },
      { id: "source", label: "原图", dataType: "image", required: true },
      {
        id: "references",
        label: "参考图",
        dataType: "image",
        multiple: true,
        maximum: 15,
      },
    ],
    outputs: [{ id: "image", label: "图片", dataType: "image" }],
    defaultConfig: {
      model: null,
      aspect_ratio: "1:1",
      size: "1K",
      quality: "2k",
      fast: true,
      background: "auto",
    },
  },
  image_inpaint: {
    type: "image_inpaint",
    label: "局部重绘",
    description: "使用遮罩编辑指定区域",
    category: "image",
    executable: true,
    family: "image",
    keywords: ["inpaint", "局部重绘", "遮罩", "蒙版", "修图"],
    icon: ImagePlus,
    width: 300,
    inputs: [
      { id: "prompt", label: "提示词", dataType: "text", required: true },
      { id: "source", label: "原图", dataType: "image", required: true },
      {
        id: "mask",
        label: "遮罩",
        dataType: "mask",
        accepts: ["mask", "image"],
        required: true,
      },
    ],
    outputs: [{ id: "image", label: "图片", dataType: "image" }],
    defaultConfig: {
      model: null,
      aspect_ratio: "1:1",
      size: "1K",
      quality: "2k",
      fast: true,
    },
  },
  image_upscale: {
    type: "image_upscale",
    label: "高清重绘",
    description: "保留主体的高质量重绘",
    category: "image",
    executable: true,
    family: "image",
    keywords: ["upscale", "4k", "高清", "重绘", "放大"],
    icon: ImagePlus,
    width: 292,
    inputs: [
      { id: "prompt", label: "提示词", dataType: "text", required: true },
      { id: "source", label: "原图", dataType: "image", required: true },
    ],
    outputs: [{ id: "image", label: "图片", dataType: "image" }],
    defaultConfig: {
      model: null,
      quality: "2k",
      size: "2K",
      fast: true,
      output_format: "webp",
    },
  },
  video_generate: {
    type: "video_generate",
    label: "视频生成",
    description: "文生视频与图生视频",
    category: "video",
    executable: true,
    family: "video",
    keywords: ["video generate", "文生视频", "图生视频", "视频", "生成"],
    icon: Video,
    width: 304,
    inputs: [
      { id: "prompt", label: "提示词", dataType: "text", required: true },
      { id: "first_frame", label: "首帧", dataType: "image" },
      {
        id: "reference_images",
        label: "参考图",
        dataType: "image",
        multiple: true,
        maximum: 9,
      },
      {
        id: "reference_videos",
        label: "参考视频",
        dataType: "video",
        multiple: true,
        maximum: 3,
      },
    ],
    outputs: [{ id: "video", label: "视频", dataType: "video" }],
    defaultConfig: {
      mode: "t2v",
      model: null,
      duration_s: 5,
      resolution: "720p",
      aspect_ratio: "16:9",
      generate_audio: true,
      seed: null,
      watermark: false,
    },
  },
  video_text_generate: {
    type: "video_text_generate",
    label: "文生视频",
    description: "只使用提示词生成视频",
    category: "video",
    executable: true,
    family: "video",
    keywords: ["t2v", "文生视频", "视频", "短片"],
    fixedVideoMode: "t2v",
    icon: Video,
    width: 292,
    inputs: [{ id: "prompt", label: "提示词", dataType: "text", required: true }],
    outputs: [{ id: "video", label: "视频", dataType: "video" }],
    defaultConfig: {
      mode: "t2v",
      model: null,
      duration_s: 5,
      resolution: "720p",
      aspect_ratio: "16:9",
      generate_audio: true,
      seed: null,
      watermark: false,
    },
  },
  video_image_generate: {
    type: "video_image_generate",
    label: "首帧生视频",
    description: "使用提示词和首帧生成视频",
    category: "video",
    executable: true,
    family: "video",
    keywords: ["i2v", "首帧", "图生视频", "视频"],
    fixedVideoMode: "i2v",
    icon: Video,
    width: 300,
    inputs: [
      { id: "prompt", label: "提示词", dataType: "text", required: true },
      { id: "first_frame", label: "首帧", dataType: "image", required: true },
    ],
    outputs: [{ id: "video", label: "视频", dataType: "video" }],
    defaultConfig: {
      mode: "i2v",
      model: null,
      duration_s: 5,
      resolution: "720p",
      aspect_ratio: "16:9",
      generate_audio: true,
      seed: null,
      watermark: false,
    },
  },
  video_reference_generate: {
    type: "video_reference_generate",
    label: "参考媒体视频",
    description: "结合图片或视频参考生成",
    category: "video",
    executable: true,
    family: "video",
    keywords: ["reference video", "参考视频", "参考图", "角色一致性"],
    fixedVideoMode: "reference",
    icon: Video,
    width: 308,
    inputs: [
      { id: "prompt", label: "提示词", dataType: "text", required: true },
      {
        id: "reference_images",
        label: "参考图",
        dataType: "image",
        multiple: true,
        maximum: 9,
      },
      {
        id: "reference_videos",
        label: "参考视频",
        dataType: "video",
        multiple: true,
        maximum: 3,
      },
    ],
    outputs: [{ id: "video", label: "视频", dataType: "video" }],
    defaultConfig: {
      mode: "reference",
      model: null,
      duration_s: 5,
      resolution: "720p",
      aspect_ratio: "16:9",
      generate_audio: true,
      seed: null,
      watermark: false,
    },
  },
  note: {
    type: "note",
    label: "备注",
    description: "画布说明",
    category: "organize",
    executable: false,
    family: "organize",
    keywords: ["note", "备注", "说明", "注释"],
    icon: FileText,
    width: 248,
    inputs: [],
    outputs: [],
    defaultConfig: { text: "", tags: [] },
  },
  frame: {
    type: "frame",
    label: "画框",
    description: "组织画布区域",
    category: "organize",
    executable: false,
    family: "organize",
    keywords: ["frame", "画框", "分组", "组织"],
    icon: Frame,
    width: 360,
    inputs: [],
    outputs: [],
    defaultConfig: {
      label: "新画框",
      collapsed: false,
      hidden_in_run: false,
      runnable_scope: true,
    },
  },
  delivery: {
    type: "delivery",
    label: "交付",
    description: "收集最终结果",
    category: "deliver",
    executable: false,
    family: "delivery",
    keywords: ["delivery", "交付", "导出", "结果"],
    icon: PackageCheck,
    width: 320,
    inputs: [
      { id: "images", label: "图片", dataType: "image", multiple: true },
      { id: "videos", label: "视频", dataType: "video", multiple: true },
    ],
    outputs: [],
    defaultConfig: {
      set_as_thumbnail: true,
      thumbnail_source_node_id: null,
    },
  },
};

export const CANVAS_NODE_TYPES = Object.keys(CANVAS_NODE_SPECS) as CanvasNodeType[];

export const CANVAS_NODE_CATALOG = [
  catalog("prompt", "prompt", "提示词", "输入创作描述", "text", ["文本", "描述"]),
  catalog("prompt_merge", "prompt_merge", "提示词合并", "组合多个文本输入", "text", ["拼接", "合并"]),
  catalog("image_asset", "image_asset", "图片素材", "上传或选择图片素材", "input", ["图片", "上传"]),
  catalog("subject_reference", "image_asset", "主体参考", "为人物或主体提供参考图", "input", ["主体", "人物", "参考"], {
    title: "主体参考",
    config: { display_name: "主体参考" },
  }),
  catalog("product_reference", "image_asset", "商品参考", "为商品提供参考图", "input", ["商品", "产品", "参考"], {
    title: "商品参考",
    config: { display_name: "商品参考" },
  }),
  catalog("style_reference", "image_asset", "风格参考", "提供构图、色彩或风格参考", "input", ["风格", "构图", "色彩"], {
    title: "风格参考",
    config: { display_name: "风格参考" },
  }),
  catalog("background_reference", "image_asset", "背景参考", "提供环境或背景参考", "input", ["背景", "场景", "环境"], {
    title: "背景参考",
    config: { display_name: "背景参考" },
  }),
  catalog("mask_asset", "mask_asset", "遮罩素材", "上传局部编辑遮罩", "input", ["mask", "蒙版"]),
  catalog("video_asset", "video_asset", "视频素材", "上传或选择视频素材", "input", ["视频", "上传"]),
  catalog("image_generate", "image_generate", "图片生成", "文生图与图生图", "image", ["文生图", "图生图"]),
  catalog("image_edit", "image_edit", "图片编辑", "基于原图进行提示词编辑", "image", ["编辑", "重绘"]),
  catalog("image_inpaint", "image_inpaint", "局部重绘", "使用遮罩重绘局部区域", "image", ["遮罩", "修图"]),
  catalog("transparent_background", "image_edit", "透明背景", "将原图编辑为透明背景", "image", ["透明", "抠图", "背景"], {
    title: "透明背景",
    config: {
      background: "transparent",
      output_format: "png",
      output_compression: null,
    },
  }),
  catalog("image_upscale", "image_upscale", "高清重绘", "保留主体的高清重绘", "image", ["高清", "放大", "重绘"]),
  catalog("image_4k_redraw", "image_upscale", "4K 高清重绘", "使用 4K 预设进行重绘", "image", ["4k", "高清", "重绘"], {
    title: "4K 高清重绘",
    config: { quality: "4k", size: "4K", fast: false },
  }),
  catalog("product_key_visual", "image_generate", "商品主视觉", "商品营销主视觉预设", "image", ["商品", "主视觉", "电商"], {
    title: "商品主视觉",
    config: { aspect_ratio: "4:5", quality: "4k", fast: false },
  }),
  catalog("video_generate", "video_generate", "视频生成", "可切换的通用视频生成", "video", ["视频", "生成"]),
  catalog("video_text_generate", "video_text_generate", "文生视频", "固定文生视频模式", "video", ["t2v", "短片"]),
  catalog("video_image_generate", "video_image_generate", "首帧视频", "固定首帧生视频模式", "video", ["i2v", "图生视频"]),
  catalog("video_reference_generate", "video_reference_generate", "参考媒体视频", "固定参考媒体生成模式", "video", ["参考图", "参考视频"]),
  catalog("character_consistency", "video_reference_generate", "人物一致性", "用参考媒体维持人物一致性", "video", ["人物", "一致性", "角色"], {
    title: "人物一致性",
    config: { aspect_ratio: "9:16" },
  }),
  catalog("vertical_short_video", "video_text_generate", "竖屏短片", "9:16 竖屏文生视频预设", "video", ["竖屏", "短视频", "9:16"], {
    title: "竖屏短片",
    config: { aspect_ratio: "9:16", duration_s: 5 },
  }),
  catalog("cinematic_widescreen_video", "video_text_generate", "电影宽屏", "16:9 电影感文生视频预设", "video", ["电影", "宽屏", "16:9"], {
    title: "电影宽屏",
    config: { aspect_ratio: "16:9", duration_s: 8 },
  }),
  catalog("note", "note", "备注", "添加画布说明", "organize", ["说明", "注释"]),
  catalog("frame", "frame", "画框", "组织画布区域", "organize", ["分组", "区域"]),
  catalog("delivery", "delivery", "交付", "收集最终图片和视频", "deliver", ["结果", "导出"]),
] as const satisfies readonly CanvasNodeCatalogItem[];

export type CanvasNodeCatalogId = (typeof CANVAS_NODE_CATALOG)[number]["id"];

let fallbackId = 0;

export function canvasUuid(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  fallbackId += 1;
  return `${prefix}-${Date.now().toString(36)}-${fallbackId.toString(36)}`;
}

export function createCanvasNode(
  type: CanvasNodeType,
  position: { x: number; y: number },
  overrides: CanvasNodeCreateOverrides = {},
): CanvasNodeDefinition {
  const spec = CANVAS_NODE_SPECS[type];
  return {
    id: overrides.id ?? canvasUuid(type),
    type,
    schema_version: 1,
    title: overrides.title ?? spec.label,
    position,
    size: overrides.size ?? { width: spec.width, height: type === "frame" ? 220 : 180 },
    parent_group_id: overrides.parent_group_id ?? null,
    config: { ...spec.defaultConfig, ...(overrides.config ?? {}) },
    ui: {
      collapsed: false,
      color_tag: null,
      preset_id: null,
      ...(overrides.ui ?? {}),
    },
  };
}

export function createCanvasNodeFromCatalog(
  catalogId: CanvasNodeCatalogId,
  position: { x: number; y: number },
  overrides: CanvasNodeCreateOverrides = {},
): CanvasNodeDefinition {
  const item = canvasNodeCatalogItem(catalogId);
  return createCanvasNode(item.type, position, mergeNodeOverrides(
    {
      ...item.overrides,
      ui: {
        ...(item.overrides?.ui ?? {}),
        preset_id: item.id,
      },
    },
    overrides,
  ));
}

export function canvasNodeCatalogItem(
  catalogId: CanvasNodeCatalogId,
): CanvasNodeCatalogItem {
  const item = findCanvasNodeCatalogItem(catalogId);
  if (!item) throw new Error(`Unknown canvas catalog item: ${catalogId}`);
  return item;
}

export function findCanvasNodeCatalogItem(
  catalogId: string,
): CanvasNodeCatalogItem | undefined {
  return CANVAS_NODE_CATALOG.find((candidate) => candidate.id === catalogId);
}

export function isCanvasNodeType(value: string): value is CanvasNodeType {
  return Object.hasOwn(CANVAS_NODE_SPECS, value);
}

export function isCanvasExecutableNodeType(type: CanvasNodeType): boolean {
  return CANVAS_NODE_SPECS[type].executable;
}

export function isCanvasVideoNodeType(type: CanvasNodeType): boolean {
  return CANVAS_NODE_SPECS[type].family === "video";
}

export function canvasVideoModeForNode(
  node: Pick<CanvasNodeDefinition, "type" | "config">,
): CanvasVideoMode | null {
  const spec = CANVAS_NODE_SPECS[node.type];
  if (spec.family !== "video") return null;
  if (spec.fixedVideoMode) return spec.fixedVideoMode;
  const configured = String(node.config.mode ?? "t2v");
  return configured === "i2v" || configured === "reference" ? configured : "t2v";
}

export function canvasFixedVideoMode(
  type: CanvasNodeType,
): CanvasVideoMode | null {
  return CANVAS_NODE_SPECS[type].fixedVideoMode ?? null;
}

export function normalizeCanvasNodeUi(value: unknown): CanvasNodeUI {
  const raw = isRecord(value) ? value : {};
  return {
    collapsed: raw.collapsed === true,
    color_tag:
      typeof raw.color_tag === "string" ? raw.color_tag : null,
    preset_id:
      typeof raw.preset_id === "string" ? raw.preset_id : null,
  };
}

export function canvasNodeUiIsValid(value: unknown): boolean {
  if (!isRecord(value) || !hasOnlyKeys(value, NODE_UI_KEYS)) return false;
  return (
    optionalBoolean(value, "collapsed") &&
    optionalNullableString(value, "color_tag", 32) &&
    optionalNullableString(value, "preset_id", 128)
  );
}

export function canvasNodeConfigIsValid(
  type: CanvasNodeType,
  value: unknown,
): boolean {
  if (!isRecord(value)) return false;
  return CANVAS_CONFIG_VALIDATORS[type](value);
}

export function findMatchingCanvasNodeCatalogItem(
  node: Pick<CanvasNodeDefinition, "type" | "config" | "ui">,
): CanvasNodeCatalogItem | undefined {
  const presetId = node.ui?.preset_id;
  if (!presetId) return undefined;
  const item = findCanvasNodeCatalogItem(presetId);
  if (!item || item.type !== node.type) return undefined;
  const expectedConfig = item.overrides?.config;
  if (!expectedConfig) return item;
  return Object.entries(expectedConfig).every(
    ([key, value]) => node.config[key] === value,
  )
    ? item
    : undefined;
}

export function canvasDefaultRoleForNode(
  node: Pick<CanvasNodeDefinition, "type" | "config" | "ui">,
): CanvasEdgeRole | null {
  const presetId = findMatchingCanvasNodeCatalogItem(node)?.id;
  if (presetId === "subject_reference") return "subject";
  if (presetId === "product_reference") return "product";
  if (presetId === "style_reference") return "style";
  if (presetId === "background_reference") return "background";
  return null;
}

function catalog(
  id: string,
  type: CanvasNodeType,
  label: string,
  description: string,
  category: CanvasNodeCategory,
  keywords: readonly string[],
  overrides?: CanvasNodeCreateOverrides,
): CanvasNodeCatalogItem {
  return { id, type, label, description, category, keywords, overrides };
}

function mergeNodeOverrides(
  base: CanvasNodeCreateOverrides,
  overrides: CanvasNodeCreateOverrides,
): CanvasNodeCreateOverrides {
  return {
    ...base,
    ...overrides,
    config: { ...(base.config ?? {}), ...(overrides.config ?? {}) },
    ui: { ...(base.ui ?? {}), ...(overrides.ui ?? {}) },
  };
}

type CanvasConfigRecord = Record<string, unknown>;
type CanvasConfigValidator = (config: CanvasConfigRecord) => boolean;

const NODE_UI_KEYS = new Set(["collapsed", "color_tag", "preset_id"]);
const IMAGE_CONFIG_KEYS = new Set([
  "model",
  "aspect_ratio",
  "size",
  "quality",
  "size_mode",
  "fixed_size",
  "render_quality",
  "count",
  "fast",
  "output_format",
  "output_compression",
  "background",
  "moderation",
]);
const VIDEO_CONFIG_KEYS = new Set([
  "mode",
  "model",
  "duration_s",
  "resolution",
  "aspect_ratio",
  "generate_audio",
  "seed",
  "watermark",
]);

const CANVAS_CONFIG_VALIDATORS: Record<
  CanvasNodeType,
  CanvasConfigValidator
> = {
  prompt: promptConfigIsValid,
  prompt_merge: promptMergeConfigIsValid,
  image_asset: imageAssetConfigIsValid,
  mask_asset: imageAssetConfigIsValid,
  video_asset: videoAssetConfigIsValid,
  image_generate: imageConfigIsValid,
  image_edit: imageConfigIsValid,
  image_inpaint: imageConfigIsValid,
  image_upscale: imageConfigIsValid,
  video_generate: (config) => videoConfigIsValid("video_generate", config),
  video_text_generate: (config) =>
    videoConfigIsValid("video_text_generate", config),
  video_image_generate: (config) =>
    videoConfigIsValid("video_image_generate", config),
  video_reference_generate: (config) =>
    videoConfigIsValid("video_reference_generate", config),
  note: noteConfigIsValid,
  frame: frameConfigIsValid,
  delivery: deliveryConfigIsValid,
};

function promptConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(config, new Set(["text", "locked"])) &&
    optionalString(config, "text", 10_000) &&
    optionalBoolean(config, "locked")
  );
}

function promptMergeConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(
      config,
      new Set(["separator", "prefix", "suffix", "trim", "dedupe"]),
    ) &&
    optionalString(config, "separator", 32) &&
    optionalString(config, "prefix", 2_000) &&
    optionalString(config, "suffix", 2_000) &&
    optionalBoolean(config, "trim") &&
    optionalBoolean(config, "dedupe")
  );
}

function imageAssetConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(config, new Set(["image_id", "display_name", "crop"])) &&
    optionalString(config, "image_id", 36) &&
    optionalNullableString(config, "display_name", 255) &&
    optionalCrop(config.crop)
  );
}

function videoAssetConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(config, new Set(["video_id", "display_name"])) &&
    optionalString(config, "video_id", 36) &&
    optionalNullableString(config, "display_name", 255)
  );
}

function imageConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(config, IMAGE_CONFIG_KEYS) &&
    optionalNullableString(config, "model", 128) &&
    optionalString(config, "aspect_ratio", 16) &&
    optionalSetValue(config, "size", new Set(["1K", "2K", "4K", "1k", "2k", "4k"])) &&
    optionalSetValue(
      config,
      "quality",
      new Set(["standard", "high", "1k", "2k", "4k"]),
    ) &&
    optionalSetValue(config, "size_mode", new Set(["auto", "fixed"])) &&
    optionalNullableString(config, "fixed_size", 32) &&
    optionalSetValue(
      config,
      "render_quality",
      new Set(["auto", "low", "medium", "high"]),
    ) &&
    optionalInteger(config, "count", 1, 10) &&
    optionalNullableBoolean(config, "fast") &&
    optionalNullableSetValue(
      config,
      "output_format",
      new Set(["png", "jpeg", "webp"]),
    ) &&
    optionalNullableInteger(config, "output_compression", 0, 100) &&
    optionalSetValue(
      config,
      "background",
      new Set(["auto", "opaque", "transparent"]),
    ) &&
    optionalSetValue(config, "moderation", new Set(["auto", "low"]))
  );
}

function videoConfigIsValid(
  type: CanvasNodeType,
  config: CanvasConfigRecord,
): boolean {
  const fixedMode = canvasFixedVideoMode(type);
  return (
    hasOnlyKeys(config, VIDEO_CONFIG_KEYS) &&
    optionalSetValue(config, "mode", new Set(["t2v", "i2v", "reference"])) &&
    (fixedMode === null || config.mode === undefined || config.mode === fixedMode) &&
    optionalNullableString(config, "model", 64) &&
    optionalSmartDuration(config, "duration_s") &&
    optionalString(config, "resolution", 16) &&
    optionalString(config, "aspect_ratio", 16) &&
    optionalBoolean(config, "generate_audio") &&
    optionalNullableInteger(
      config,
      "seed",
      Number.MIN_SAFE_INTEGER,
      Number.MAX_SAFE_INTEGER,
    ) &&
    optionalBoolean(config, "watermark")
  );
}

function noteConfigIsValid(config: CanvasConfigRecord): boolean {
  if (
    !hasOnlyKeys(config, new Set(["text", "tags"])) ||
    !optionalString(config, "text", 20_000)
  ) {
    return false;
  }
  if (config.tags === undefined) return true;
  return (
    Array.isArray(config.tags) &&
    config.tags.length <= 12 &&
    config.tags.every(
      (tag) => typeof tag === "string" && tag.trim().length > 0 && tag.trim().length <= 32,
    )
  );
}

function frameConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(
      config,
      new Set(["label", "collapsed", "hidden_in_run", "runnable_scope"]),
    ) &&
    optionalString(config, "label", 255) &&
    optionalBoolean(config, "collapsed") &&
    optionalBoolean(config, "hidden_in_run") &&
    optionalBoolean(config, "runnable_scope")
  );
}

function deliveryConfigIsValid(config: CanvasConfigRecord): boolean {
  return (
    hasOnlyKeys(
      config,
      new Set(["set_as_thumbnail", "thumbnail_source_node_id"]),
    ) &&
    optionalBoolean(config, "set_as_thumbnail") &&
    optionalNullableEntityId(config, "thumbnail_source_node_id")
  );
}

function isRecord(value: unknown): value is CanvasConfigRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasOnlyKeys(
  value: CanvasConfigRecord,
  allowed: ReadonlySet<string>,
): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function optionalString(
  config: CanvasConfigRecord,
  key: string,
  maxLength: number,
): boolean {
  return (
    config[key] === undefined ||
    (typeof config[key] === "string" &&
      (config[key] as string).length <= maxLength)
  );
}

function optionalNullableString(
  config: CanvasConfigRecord,
  key: string,
  maxLength: number,
): boolean {
  return config[key] === null || optionalString(config, key, maxLength);
}

function optionalBoolean(config: CanvasConfigRecord, key: string): boolean {
  return config[key] === undefined || typeof config[key] === "boolean";
}

function optionalNullableBoolean(
  config: CanvasConfigRecord,
  key: string,
): boolean {
  return config[key] === null || optionalBoolean(config, key);
}

function optionalSetValue(
  config: CanvasConfigRecord,
  key: string,
  values: ReadonlySet<string>,
): boolean {
  return config[key] === undefined || values.has(String(config[key]));
}

function optionalNullableSetValue(
  config: CanvasConfigRecord,
  key: string,
  values: ReadonlySet<string>,
): boolean {
  return config[key] === null || optionalSetValue(config, key, values);
}

function optionalInteger(
  config: CanvasConfigRecord,
  key: string,
  minimum: number,
  maximum: number,
): boolean {
  const value = config[key];
  return (
    value === undefined ||
    (Number.isInteger(value) &&
      Number(value) >= minimum &&
      Number(value) <= maximum)
  );
}

function optionalNullableInteger(
  config: CanvasConfigRecord,
  key: string,
  minimum: number,
  maximum: number,
): boolean {
  return config[key] === null || optionalInteger(config, key, minimum, maximum);
}

function optionalSmartDuration(
  config: CanvasConfigRecord,
  key: string,
): boolean {
  const value = config[key];
  return (
    value === undefined ||
    value === -1 ||
    (Number.isInteger(value) && Number(value) >= 3 && Number(value) <= 15)
  );
}

function optionalCrop(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (!isRecord(value) || !hasOnlyKeys(value, new Set(["x", "y", "width", "height"]))) {
    return false;
  }
  const x = Number(value.x);
  const y = Number(value.y);
  const width = Number(value.width);
  const height = Number(value.height);
  return (
    [x, y, width, height].every(Number.isFinite) &&
    x >= 0 &&
    y >= 0 &&
    width > 0 &&
    height > 0 &&
    x + width <= 1 &&
    y + height <= 1
  );
}

function optionalNullableEntityId(
  config: CanvasConfigRecord,
  key: string,
): boolean {
  const value = config[key];
  return (
    value === undefined ||
    value === null ||
    (typeof value === "string" &&
      /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/.test(value))
  );
}
