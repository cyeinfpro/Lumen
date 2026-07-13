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
  CanvasNodeDefinition,
  CanvasNodeType,
} from "#canvas-types";

export interface CanvasPortSpec {
  id: string;
  label: string;
  dataType: CanvasDataType;
  multiple?: boolean;
  required?: boolean;
  accepts?: CanvasDataType[];
}

export interface CanvasNodeSpec {
  type: CanvasNodeType;
  label: string;
  description: string;
  icon: LucideIcon;
  width: number;
  inputs: CanvasPortSpec[];
  outputs: CanvasPortSpec[];
  defaultConfig: Record<string, unknown>;
}

export const CANVAS_NODE_SPECS: Record<CanvasNodeType, CanvasNodeSpec> = {
  prompt: {
    type: "prompt",
    label: "提示词",
    description: "文本输入",
    icon: MessageSquareText,
    width: 260,
    inputs: [],
    outputs: [{ id: "text", label: "文本", dataType: "text" }],
    defaultConfig: { text: "", locked: false },
  },
  image_asset: {
    type: "image_asset",
    label: "图片素材",
    description: "上传或资产图片",
    icon: FileImage,
    width: 248,
    inputs: [],
    outputs: [{ id: "image", label: "图片", dataType: "image" }],
    defaultConfig: { image_id: "", display_name: "", crop: null },
  },
  video_asset: {
    type: "video_asset",
    label: "视频素材",
    description: "已有视频",
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
    icon: ImagePlus,
    width: 292,
    inputs: [
      { id: "prompt", label: "提示词", dataType: "text", required: true },
      { id: "references", label: "参考图", dataType: "image", multiple: true },
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
  video_generate: {
    type: "video_generate",
    label: "视频生成",
    description: "文生视频与图生视频",
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
      },
      {
        id: "reference_videos",
        label: "参考视频",
        dataType: "video",
        multiple: true,
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
  note: {
    type: "note",
    label: "备注",
    description: "画布说明",
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
  overrides: Partial<CanvasNodeDefinition> = {},
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
      ...(overrides.ui ?? {}),
    },
  };
}
