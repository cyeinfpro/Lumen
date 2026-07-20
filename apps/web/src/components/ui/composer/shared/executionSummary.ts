import type { AspectRatio, Quality, RenderQualityChoice } from "@/lib/types";
import type { ComposerAttachmentRole } from "./attachmentRoles";

type ComposerMode = "chat" | "image";

const QUALITY_LABELS: Record<Quality, string> = {
  "1k": "1K",
  "2k": "2K",
  "4k": "4K",
};

const RENDER_QUALITY_LABELS: Record<RenderQualityChoice, string> = {
  low: "低",
  medium: "中",
  high: "高",
};

const REASONING_LABELS: Record<string, string> = {
  none: "最快",
  minimal: "极简",
  low: "低思考",
  medium: "中思考",
  high: "高思考",
  xhigh: "深度思考",
};

export interface ComposerExecutionSummary {
  taskLabel: string;
  parts: string[];
  text: string;
  tone: "chat" | "image";
  costWarning: boolean;
}

function imageTaskLabel(input: {
  attachmentCount: number;
  maskActive: boolean;
  attachmentRoles: ComposerAttachmentRole[];
}): string {
  if (input.maskActive) return "局部修改";
  if (input.attachmentCount === 0) return "文生图";
  if (input.attachmentRoles.includes("edit_target")) return "编辑图生图";
  return "参考图生图";
}

function executionTaskLabel(
  mode: ComposerMode,
  attachmentCount: number,
  input: {
    attachmentCount: number;
    maskActive: boolean;
    attachmentRoles: ComposerAttachmentRole[];
  },
): string {
  if (mode === "image") return imageTaskLabel(input);
  return attachmentCount > 0 ? "识图问答" : "文本对话";
}

function imageExecutionParts(input: {
  attachmentCount: number;
  outputCount: number;
  aspect: AspectRatio;
  quality: Quality;
  renderQuality: RenderQualityChoice;
  fast: boolean;
}): string[] {
  const count = Math.max(1, Math.min(16, input.outputCount || 1));
  const parts = [
    `${count} 张`,
    input.aspect,
    QUALITY_LABELS[input.quality],
    RENDER_QUALITY_LABELS[input.renderQuality],
    input.fast ? "Fast" : "标准",
  ];
  if (input.attachmentCount > 0) parts.push(`${input.attachmentCount} 张参考`);
  return parts;
}

function chatExecutionParts(input: {
  attachmentCount: number;
  fast: boolean;
  reasoningEffort?: string;
  webSearch?: boolean;
  fileSearch?: boolean;
  codeInterpreter?: boolean;
  imageGeneration?: boolean;
}): string[] {
  const parts: string[] = [];
  if (input.attachmentCount > 0) parts.push(`${input.attachmentCount} 张图`);
  const reasoning = input.reasoningEffort
    ? REASONING_LABELS[input.reasoningEffort]
    : null;
  if (reasoning) parts.push(reasoning);
  parts.push(input.fast ? "Fast" : "标准");
  if (input.webSearch) parts.push("联网");
  if (input.fileSearch) parts.push("文件");
  if (input.codeInterpreter) parts.push("代码");
  if (input.imageGeneration) parts.push("可出图");
  return parts;
}

export function buildComposerExecutionSummary(input: {
  mode: ComposerMode;
  attachmentCount: number;
  attachmentRoles: ComposerAttachmentRole[];
  outputCount: number;
  aspect: AspectRatio;
  quality: Quality;
  renderQuality: RenderQualityChoice;
  fast: boolean;
  maskActive: boolean;
  costLabel?: string | null;
  costWarning?: boolean;
  reasoningEffort?: string;
  webSearch?: boolean;
  fileSearch?: boolean;
  codeInterpreter?: boolean;
  imageGeneration?: boolean;
}): ComposerExecutionSummary {
  const tone = input.mode === "image" ? "image" : "chat";
  const taskLabel = executionTaskLabel(input.mode, input.attachmentCount, input);
  const parts =
    input.mode === "image"
      ? imageExecutionParts(input)
      : chatExecutionParts(input);
  if (input.costLabel) parts.push(input.costLabel);

  const text = ["将执行：" + taskLabel, ...parts].join(" · ");
  return {
    taskLabel,
    parts,
    text,
    tone,
    costWarning: Boolean(input.costWarning),
  };
}
