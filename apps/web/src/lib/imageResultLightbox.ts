"use client";

import type {
  LightboxItem,
  LightboxParamBag,
  LightboxProviderAttempt,
} from "@/components/ui/lightbox/types";
import { imageBinaryUrl, imageVariantUrl } from "@/lib/apiClient";
import type { GeneratedImage, Generation } from "@/lib/types";

type ImageResultLightboxOptions = {
  prompt?: string;
  url?: string | null;
  previewUrl?: string | null;
  thumbUrl?: string | null;
  type?: string;
  source?: string;
  sourceType?: string;
  sourceId?: string | null;
  conversationId?: string | null;
  messageId?: string | null;
  createdAt?: number | string | null;
};

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function text(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

function recordText(
  record: Record<string, unknown> | null | undefined,
  keys: readonly string[],
): string | null {
  if (!record) return null;
  for (const key of keys) {
    const value = text(record[key]);
    if (value) return value;
  }
  return null;
}

function recordObject(
  record: Record<string, unknown> | null | undefined,
  keys: readonly string[],
): Record<string, unknown> | null {
  if (!record) return null;
  for (const key of keys) {
    const value = asRecord(record[key]);
    if (value) return value;
  }
  return null;
}

function recordArray<T = unknown>(
  record: Record<string, unknown> | null | undefined,
  keys: readonly string[],
): T[] | null {
  if (!record) return null;
  for (const key of keys) {
    const value = record[key];
    if (Array.isArray(value)) return value as T[];
  }
  return null;
}

function isoFromMaybeMs(value: number | string | null | undefined): string | undefined {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return new Date(value).toISOString();
  }
  return undefined;
}

function validMediaUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const candidate = value.trim();
  if (!candidate || /^(?:null|undefined)$/i.test(candidate)) return null;
  if (/^data:image\//i.test(candidate)) return candidate;
  try {
    const parsed = new URL(candidate, "https://lumen.invalid");
    return ["http:", "https:", "blob:"].includes(parsed.protocol)
      ? candidate
      : null;
  } catch {
    return null;
  }
}

function firstMediaUrl(...candidates: unknown[]): string | null {
  for (const candidate of candidates) {
    const url = validMediaUrl(candidate);
    if (url) return url;
  }
  return null;
}

function explainabilityMetadata(
  gen: Generation,
  image: GeneratedImage,
): Record<string, unknown> {
  const metadata = { ...(image.metadata_jsonb ?? {}) };
  const diagnostics: LightboxParamBag | null =
    asRecord(gen.diagnostics) ??
    asRecord(image.diagnostics) ??
    recordObject(metadata, ["generation_diagnostics", "diagnostics"]);
  const revisedPrompt =
    gen.revised_prompt ??
    image.revised_prompt ??
    recordText(metadata, ["revised_prompt", "model_revised_prompt"]);
  const requestedParams =
    gen.requested_params ??
    gen.request_params ??
    image.requested_params ??
    image.request_params ??
    recordObject(metadata, ["requested_params", "request_params"]);
  const effectiveParams =
    gen.effective_params ??
    gen.actual_params ??
    image.effective_params ??
    image.actual_params ??
    recordObject(metadata, ["effective_params", "actual_params"]);
  const attempts =
    gen.provider_attempts ??
    image.provider_attempts ??
    recordArray(metadata, ["provider_attempts"]);

  if (diagnostics && metadata.generation_diagnostics == null) {
    metadata.generation_diagnostics = diagnostics;
  }
  if (revisedPrompt && metadata.revised_prompt == null) {
    metadata.revised_prompt = revisedPrompt;
  }
  if (requestedParams && metadata.requested_params == null) {
    metadata.requested_params = requestedParams;
  }
  if (effectiveParams && metadata.effective_params == null) {
    metadata.effective_params = effectiveParams;
  }
  if (attempts && metadata.provider_attempts == null) {
    metadata.provider_attempts = attempts;
  }
  if (image.parent_image_id && metadata.parent_image_id == null) {
    metadata.parent_image_id = image.parent_image_id;
  }
  if (image.from_generation_id && metadata.from_generation_id == null) {
    metadata.from_generation_id = image.from_generation_id;
  }
  return metadata;
}

function actionSourceFor(
  gen: Generation,
  image: GeneratedImage,
  metadata: Record<string, unknown>,
): string {
  return (
    recordText(metadata, ["action_source", "generation_action", "action"]) ??
    (gen.action === "edit" || image.parent_image_id ? "edit" : "generate")
  );
}

function sourceFor(
  metadata: Record<string, unknown>,
  options: ImageResultLightboxOptions,
): { source: string; sourceType: string; sourceId: string | null } {
  const source =
    options.source ??
    recordText(metadata, ["source", "source_type", "origin"]) ??
    "chat";
  const sourceType =
    options.sourceType ??
    recordText(metadata, ["source_type", "origin_type"]) ??
    source;
  const sourceId =
    options.sourceId ??
    recordText(metadata, ["source_id", "workflow_run_id", "message_id"]) ??
    null;
  return { source, sourceType, sourceId };
}

function mediaUrls(
  image: GeneratedImage,
  options: ImageResultLightboxOptions,
): Pick<LightboxItem, "url" | "previewUrl" | "thumbUrl"> {
  return {
    url: firstMediaUrl(options.url) ?? imageBinaryUrl(image.id),
    previewUrl:
      firstMediaUrl(options.previewUrl, image.display_url, image.preview_url) ??
      imageVariantUrl(image.id, "display2048"),
    thumbUrl:
      firstMediaUrl(options.thumbUrl, image.thumb_url, image.preview_url) ??
      imageVariantUrl(image.id, "thumb256"),
  };
}

export function imageResultToLightboxItem(
  gen: Generation,
  image: GeneratedImage,
  options: ImageResultLightboxOptions = {},
): LightboxItem {
  const metadata = explainabilityMetadata(gen, image);
  const { source, sourceType, sourceId } = sourceFor(metadata, options);
  const media = mediaUrls(image, options);
  const diagnostics: LightboxParamBag | null =
    asRecord(gen.diagnostics) ??
    asRecord(image.diagnostics) ??
    recordObject(metadata, ["generation_diagnostics", "diagnostics"]);
  const requestedParams =
    gen.requested_params ??
    gen.request_params ??
    image.requested_params ??
    image.request_params ??
    recordObject(metadata, ["requested_params", "request_params"]);
  const effectiveParams =
    gen.effective_params ??
    gen.actual_params ??
    image.effective_params ??
    image.actual_params ??
    recordObject(metadata, ["effective_params", "actual_params"]);
  const providerAttempts: LightboxProviderAttempt[] | undefined =
    gen.provider_attempts ??
    image.provider_attempts ??
    recordArray<LightboxProviderAttempt>(metadata, ["provider_attempts"]) ??
    undefined;
  const parentGenerationId =
    gen.parent_generation_id ??
    recordText(metadata, ["parent_generation_id", "parent_task_id"]);
  const fromGenerationId =
    image.from_generation_id ??
    recordText(metadata, ["from_generation_id", "generation_id"]) ??
    gen.id;

  return {
    id: image.id,
    ...media,
    prompt: options.prompt ?? gen.prompt,
    width: image.width,
    height: image.height,
    aspect_ratio: gen.aspect_ratio,
    size_actual: image.size_actual || `${image.width}x${image.height}`,
    size_requested: image.size_requested ?? gen.size_requested,
    mime: image.mime,
    filename: image.filename,
    type: options.type ?? "generated-image",
    created_at: isoFromMaybeMs(options.createdAt ?? gen.finished_at ?? gen.started_at),
    revised_prompt:
      gen.revised_prompt ??
      image.revised_prompt ??
      recordText(metadata, ["revised_prompt", "model_revised_prompt"]),
    requested_params: requestedParams,
    request_params: requestedParams,
    effective_params: effectiveParams,
    actual_params: effectiveParams,
    diagnostics,
    provider_attempts: providerAttempts,
    source,
    source_type: sourceType,
    source_id: sourceId,
    parent_image_id:
      image.parent_image_id ??
      recordText(metadata, ["parent_image_id", "source_image_id"]),
    parent_generation_id: parentGenerationId,
    from_generation_id: fromGenerationId,
    generation_id: gen.id,
    message_id: options.messageId ?? gen.message_id,
    conversation_id: options.conversationId ?? null,
    action_source: actionSourceFor(gen, image, metadata),
    generation_action: gen.action,
    metadata,
  };
}
