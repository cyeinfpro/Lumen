import { MAX_COMPOSER_ATTACHMENTS } from "../../lib/attachmentLimits";
import {
  MAX_PROMPT_CHARS,
  PROMPT_TOO_LONG_MESSAGE,
  isPromptTooLong,
} from "../../lib/promptLimits";
import { remapPromptImageMentions } from "../../lib/promptImageMentions";
import { nearestAspectRatio } from "../../lib/sizing";
import type {
  AspectRatio,
  AttachmentImage,
  Intent,
} from "../../lib/types";
import { uuid } from "../../lib/utils";
import { clonePlainValue } from "./history";
import { clampImageCount, DEFAULT_PARAMS } from "./imageParams";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
  ComposerMode,
  ComposerState,
} from "./types";

const DEFAULT_COMPOSER: ComposerState = {
  text: "",
  attachments: [],
  mode: "chat",
  params: DEFAULT_PARAMS,
  forceIntent: undefined,
  reasoningEffort: "high",
  fast: true,
  webSearch: true,
  fileSearch: false,
  codeInterpreter: false,
  imageGeneration: false,
  mask: null,
};

export type ComposerActions = Pick<
  ChatState,
  | "setComposerError"
  | "setText"
  | "setMode"
  | "setForceIntent"
  | "setAspectRatio"
  | "setSizeMode"
  | "setFixedSize"
  | "setQuality"
  | "setRenderQuality"
  | "setImageCount"
  | "setReasoningEffort"
  | "setFast"
  | "setWebSearch"
  | "setFileSearch"
  | "setCodeInterpreter"
  | "setImageGeneration"
  | "addAttachment"
  | "removeAttachment"
  | "moveAttachment"
  | "setMask"
  | "clearMask"
  | "clearComposer"
  | "promoteImageToReference"
>;

type ComposerActionDependencies = {
  createInitialComposer: () => ComposerState;
  markFastTouched: () => void;
};

export function createComposerState(
  runtimeFastDefault: boolean | null,
): ComposerState {
  return {
    ...DEFAULT_COMPOSER,
    fast: runtimeFastDefault ?? DEFAULT_COMPOSER.fast,
    attachments: [],
    params: { ...DEFAULT_PARAMS },
    mask: null,
  };
}

export function cloneComposerState(composer: ComposerState): ComposerState {
  const attachments = clonePlainValue(composer.attachments);
  const mask =
    composer.mask &&
    attachments.some(
      (attachment) => attachment.id === composer.mask?.target_attachment_id,
    )
      ? clonePlainValue(composer.mask)
      : null;
  return {
    ...composer,
    attachments,
    params: clonePlainValue(composer.params),
    mask,
  };
}

function samePlainRecord(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): boolean {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  for (const key of keys) {
    if (!Object.is(left[key], right[key])) return false;
  }
  return true;
}

function hasSameComposerPreferences(
  composer: ComposerState,
  baseline: ComposerState,
): boolean {
  return (
    composer.mode === baseline.mode &&
    samePlainRecord(
      composer.params as unknown as Record<string, unknown>,
      baseline.params as unknown as Record<string, unknown>,
    ) &&
    composer.reasoningEffort === baseline.reasoningEffort &&
    composer.fast === baseline.fast &&
    composer.webSearch === baseline.webSearch &&
    composer.fileSearch === baseline.fileSearch &&
    composer.codeInterpreter === baseline.codeInterpreter &&
    composer.imageGeneration === baseline.imageGeneration
  );
}

export function isResetComposerDraft(
  composer: ComposerState,
  preferenceBaseline?: ComposerState,
): boolean {
  return (
    composer.text === "" &&
    composer.attachments.length === 0 &&
    composer.mask === null &&
    composer.forceIntent === undefined &&
    (!preferenceBaseline ||
      hasSameComposerPreferences(composer, preferenceBaseline))
  );
}

export function isRetryComposerDraft(
  composer: ComposerState,
  retryText: string,
  attachmentIds: string[],
  preferenceBaseline?: ComposerState,
): boolean {
  return (
    composer.text === retryText &&
    composer.attachments.length === attachmentIds.length &&
    composer.attachments.every(
      (attachment, index) => attachment.id === attachmentIds[index],
    ) &&
    (!preferenceBaseline ||
      hasSameComposerPreferences(composer, preferenceBaseline))
  );
}

export function isTemporaryInpaintComposerDraft(
  composer: ComposerState,
  text: string,
  attachmentId: string,
  preferenceBaseline?: ComposerState,
): boolean {
  return (
    composer.text === text &&
    composer.attachments.length === 1 &&
    composer.attachments[0]?.id === attachmentId &&
    composer.mask?.target_attachment_id === attachmentId &&
    (!preferenceBaseline ||
      hasSameComposerPreferences(composer, preferenceBaseline))
  );
}

export function hasComposerContent(composer: ComposerState): boolean {
  return Boolean(composer.text.trim() || composer.attachments.length > 0);
}

export function didPromptNeedTrimming(
  originalPrompt: string,
  appendedPrompt: string,
): boolean {
  return (
    originalPrompt.length > 0 &&
    originalPrompt.trim().length + appendedPrompt.trim().length + 2 >
      MAX_PROMPT_CHARS
  );
}

export function inpaintValidationError(
  text: string,
  sourceImageId: string,
  sourceSrc: string,
): string | null {
  if (!text) return "修改内容未填";
  if (isPromptTooLong(text)) return PROMPT_TOO_LONG_MESSAGE;
  return sourceImageId && sourceSrc ? null : "图片信息不完整，无法发起局部修改";
}

export function inpaintAspectRatio(
  sourceWidth: number | undefined,
  sourceHeight: number | undefined,
): AspectRatio | null {
  return sourceWidth && sourceHeight
    ? nearestAspectRatio(sourceWidth, sourceHeight)
    : null;
}

export function resolveIntent(
  mode: ComposerMode,
  hasAttachments: boolean,
  force?: ComposerState["forceIntent"],
): Exclude<Intent, "auto"> {
  if (force === "chat") return hasAttachments ? "vision_qa" : "chat";
  if (force === "image") {
    return hasAttachments ? "image_to_image" : "text_to_image";
  }
  if (mode === "image") {
    return hasAttachments ? "image_to_image" : "text_to_image";
  }
  return hasAttachments ? "vision_qa" : "chat";
}

export function createComposerActions(
  set: ChatStateSetter,
  get: ChatStateGetter,
  dependencies: ComposerActionDependencies,
): ComposerActions {
  return {
    setComposerError: (error) => set({ composerError: error }),
    setText: (text) =>
      set((state) => ({ composer: { ...state.composer, text } })),
    setMode: (mode) =>
      set((state) => ({
        composer: { ...state.composer, mode, forceIntent: undefined },
      })),
    setForceIntent: (forceIntent) =>
      set((state) => ({
        composer: { ...state.composer, forceIntent },
      })),
    setAspectRatio: (aspectRatio) =>
      set((state) => ({
        composer: {
          ...state.composer,
          params: { ...state.composer.params, aspect_ratio: aspectRatio },
        },
      })),
    setSizeMode: (sizeMode) =>
      set((state) => ({
        composer: {
          ...state.composer,
          params: { ...state.composer.params, size_mode: sizeMode },
        },
      })),
    setFixedSize: (fixedSize) =>
      set((state) => ({
        composer: {
          ...state.composer,
          params: { ...state.composer.params, fixed_size: fixedSize },
        },
      })),
    setQuality: (quality) =>
      set((state) => ({
        composer: {
          ...state.composer,
          params: { ...state.composer.params, quality },
        },
      })),
    setRenderQuality: (renderQuality) =>
      set((state) => ({
        composer: {
          ...state.composer,
          params: {
            ...state.composer.params,
            render_quality: renderQuality,
          },
        },
      })),
    setImageCount: (count) =>
      set((state) => ({
        composer: {
          ...state.composer,
          params: {
            ...state.composer.params,
            count: clampImageCount(count),
          },
        },
      })),
    setReasoningEffort: (reasoningEffort) =>
      set((state) => ({
        composer: { ...state.composer, reasoningEffort },
      })),
    setFast: (fast) => {
      dependencies.markFastTouched();
      set((state) => ({ composer: { ...state.composer, fast } }));
    },
    setWebSearch: (webSearch) =>
      set((state) => ({
        composer: { ...state.composer, webSearch },
      })),
    setFileSearch: (fileSearch) =>
      set((state) => ({
        composer: { ...state.composer, fileSearch },
      })),
    setCodeInterpreter: (codeInterpreter) =>
      set((state) => ({
        composer: { ...state.composer, codeInterpreter },
      })),
    setImageGeneration: (imageGeneration) =>
      set((state) => ({
        composer: { ...state.composer, imageGeneration },
      })),
    addAttachment: (attachment) =>
      set((state) => {
        const previousAttachments = state.composer.attachments;
        if (
          previousAttachments.some(
            (existing) => existing.id === attachment.id,
          )
        ) {
          return state;
        }
        if (previousAttachments.length >= MAX_COMPOSER_ATTACHMENTS) {
          return {
            composerError: `最多添加 ${MAX_COMPOSER_ATTACHMENTS} 张参考图`,
          };
        }
        const nextAttachments = [...previousAttachments, attachment];
        const nextMask =
          previousAttachments.length === 0 ? state.composer.mask : null;
        return {
          composer: {
            ...state.composer,
            text: remapPromptImageMentions(
              state.composer.text,
              previousAttachments,
              nextAttachments,
            ),
            attachments: nextAttachments,
            mask: nextMask,
          },
        };
      }),
    removeAttachment: (id) =>
      set((state) => {
        const previousAttachments = state.composer.attachments;
        const nextAttachments = previousAttachments.filter(
          (attachment) => attachment.id !== id,
        );
        if (nextAttachments.length === previousAttachments.length) return state;
        const nextMask =
          state.composer.mask &&
          nextAttachments.some(
            (attachment) =>
              attachment.id === state.composer.mask!.target_attachment_id,
          )
            ? state.composer.mask
            : null;
        return {
          composer: {
            ...state.composer,
            text: remapPromptImageMentions(
              state.composer.text,
              previousAttachments,
              nextAttachments,
            ),
            attachments: nextAttachments,
            mask: nextMask,
          },
        };
      }),
    moveAttachment: (id, targetId) =>
      set((state) => {
        if (id === targetId) return state;
        const previousAttachments = state.composer.attachments;
        const from = previousAttachments.findIndex(
          (attachment) => attachment.id === id,
        );
        const to = previousAttachments.findIndex(
          (attachment) => attachment.id === targetId,
        );
        if (from < 0 || to < 0 || from === to) return state;
        const nextAttachments = [...previousAttachments];
        const [moved] = nextAttachments.splice(from, 1);
        if (!moved) return state;
        nextAttachments.splice(to, 0, moved);
        return {
          composer: {
            ...state.composer,
            text: remapPromptImageMentions(
              state.composer.text,
              previousAttachments,
              nextAttachments,
            ),
            attachments: nextAttachments,
          },
        };
      }),
    setMask: (mask) =>
      set((state) => ({ composer: { ...state.composer, mask } })),
    clearMask: () =>
      set((state) => ({ composer: { ...state.composer, mask: null } })),
    clearComposer: () =>
      set((state) => ({
        composer: {
          ...dependencies.createInitialComposer(),
          mode: state.composer.mode,
          params: state.composer.params,
          reasoningEffort: state.composer.reasoningEffort,
          fast: state.composer.fast,
          webSearch: state.composer.webSearch,
          fileSearch: state.composer.fileSearch,
          codeInterpreter: state.composer.codeInterpreter,
          imageGeneration: state.composer.imageGeneration,
        },
      })),
    promoteImageToReference: (imageId) => {
      const image = get().imagesById[imageId];
      if (!image) return;
      const attachment: AttachmentImage = {
        id: uuid(),
        kind: "generated",
        data_url: image.data_url,
        mime: "image/png",
        width: image.width,
        height: image.height,
        source_image_id: image.id,
      };
      set((state) => {
        const atLimit =
          state.composer.attachments.length >= MAX_COMPOSER_ATTACHMENTS;
        return {
          composerError: atLimit
            ? `最多添加 ${MAX_COMPOSER_ATTACHMENTS} 张参考图`
            : state.composerError,
          composer: {
            ...state.composer,
            text: atLimit
              ? state.composer.text
              : remapPromptImageMentions(
                  state.composer.text,
                  state.composer.attachments,
                  [attachment, ...state.composer.attachments],
                ),
            attachments: atLimit
              ? state.composer.attachments
              : [attachment, ...state.composer.attachments],
            mode: "image",
            mask: atLimit ? state.composer.mask : null,
          },
        };
      });
    },
  };
}
