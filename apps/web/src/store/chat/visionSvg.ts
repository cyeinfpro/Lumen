import type { Intent } from "../../lib/types";
import { cloneComposerState } from "./composerSlice";
import type { ComposerState } from "./types";

export function createVisionSvgSnapshot(composer: ComposerState): ComposerState {
  return {
    ...cloneComposerState(composer),
    mode: "chat",
    forceIntent: undefined,
    artifactFormat: "svg",
    fast: false,
    webSearch: false,
    fileSearch: false,
    codeInterpreter: false,
    imageGeneration: false,
  };
}

export function visionSvgSendError(composer: ComposerState, intent: Exclude<Intent, "auto">): string | null {
  if (!composer.artifactFormat) return null;
  if (composer.artifactFormat !== "svg" || intent !== "vision_qa") return "视觉重建 SVG 必须使用独立视觉流程";
  if (composer.attachments.length !== 1 || !["image/png", "image/jpeg", "image/webp"].includes(composer.attachments[0]?.mime)) return "视觉重建 SVG 需要且仅支持一张静态 PNG/JPEG/WebP 参考图";
  if (composer.mask || composer.markup) return "请先清除蒙版或标注，再使用视觉重建 SVG；参考图会保留";
  if (composer.fast || composer.webSearch || composer.fileSearch || composer.codeInterpreter || composer.imageGeneration) return "视觉重建 SVG 不使用快速模型、搜索、代码或图像工具";
  return null;
}
