import { imageBinaryUrl } from "../../lib/apiClient";
import type {
  BackendGeneration,
  BackendImageMeta,
  BackendMessage,
  MessageListResponse,
} from "../../lib/apiClient";
import type {
  AssistantMessage,
  AttachmentImage,
  Generation,
  GeneratedImage,
  Message,
} from "../../lib/types";
import {
  coerceGenerationStage,
  coerceGenerationStatus,
} from "../chatGenerationEvents";
import {
  aggregateGenerationStatus,
  completionToolGenerationId,
  generationExplainabilityFromBackend,
  generationIdsOfMessage,
  mergeExplainabilityIntoImage,
  preferredGenerationSnapshot,
  type GenerationExplainabilityMeta,
} from "./generationSlice";
import { DEFAULT_PARAMS } from "./imageParams";
import {
  adaptBackendAssistantMessage,
  adaptBackendUserMessage,
  coerceAssistantStatus,
} from "./messageAdapters";
import {
  billingMetaFromPayload,
  coerceAspectRatio,
  isoToMs,
  stringArray,
  stringOrNull,
} from "./payload";

const BASE64_EVICTION_MIN_CHARS = 1024;

export interface ConversationHistoryCacheEntry {
  messages: Message[];
  generations: Record<string, Generation>;
  imagesById: Record<string, GeneratedImage>;
  messagesCursor: string | null;
  messagesHasMore: boolean;
  updatedAt: number;
}

export interface MessageListMaterialization {
  imageIds: string[];
  generations: Generation[];
  completionMessages: Array<{
    completionId: string;
    messageId: string;
  }>;
  messages: Message[];
}

export interface BuiltMessageListState {
  messages: Message[];
  generations: Record<string, Generation>;
  imagesById: Record<string, GeneratedImage>;
  materialization: MessageListMaterialization;
}

export function clonePlainValue<T>(value: T): T {
  if (typeof structuredClone === "function") {
    try {
      return structuredClone(value);
    } catch {
      // Fall through to the manual plain-object clone below.
    }
  }
  if (Array.isArray(value)) {
    return value.map((item) => clonePlainValue(item)) as T;
  }
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, nested] of Object.entries(value)) {
      out[key] = clonePlainValue(nested);
    }
    return out as T;
  }
  return value;
}

export function cloneConversationHistoryCacheEntry(
  entry: ConversationHistoryCacheEntry,
): ConversationHistoryCacheEntry {
  return {
    messages: clonePlainValue(entry.messages),
    generations: clonePlainValue(entry.generations),
    imagesById: clonePlainValue(entry.imagesById),
    messagesCursor: entry.messagesCursor,
    messagesHasMore: entry.messagesHasMore,
    updatedAt: entry.updatedAt,
  };
}

export function isEvictableDataUrl(src: string | undefined): boolean {
  return (
    typeof src === "string" &&
    src.startsWith("data:") &&
    src.length >= BASE64_EVICTION_MIN_CHARS
  );
}

function pickConversationImages(
  messages: Message[],
  generations: Record<string, Generation>,
  imagesById: Record<string, GeneratedImage>,
): Record<string, GeneratedImage> {
  const imageIds = new Set<string>();
  for (const message of messages) {
    if (message.role === "user") {
      for (const attachment of message.attachments) {
        if (attachment.id) imageIds.add(attachment.id);
        if (attachment.source_image_id) {
          imageIds.add(attachment.source_image_id);
        }
      }
      continue;
    }
    for (const generationId of generationIdsOfMessage(message)) {
      const imageId = generations[generationId]?.image?.id;
      if (imageId) imageIds.add(imageId);
    }
  }

  const picked: Record<string, GeneratedImage> = {};
  for (const imageId of imageIds) {
    const image = imagesById[imageId];
    if (image) picked[imageId] = image;
  }
  return picked;
}

function pickConversationGenerations(
  messages: Message[],
  generations: Record<string, Generation>,
): Record<string, Generation> {
  const picked: Record<string, Generation> = {};
  for (const message of messages) {
    if (message.role !== "assistant") continue;
    for (const generationId of generationIdsOfMessage(message)) {
      const generation = generations[generationId];
      if (generation) picked[generationId] = generation;
    }
  }
  return picked;
}

export function makeConversationHistoryCacheEntry(
  messages: Message[],
  generations: Record<string, Generation>,
  imagesById: Record<string, GeneratedImage>,
  messagesCursor: string | null,
  messagesHasMore: boolean,
  now = Date.now(),
): ConversationHistoryCacheEntry {
  const pickedGenerations = pickConversationGenerations(messages, generations);
  return {
    messages: clonePlainValue(messages),
    generations: clonePlainValue(pickedGenerations),
    imagesById: clonePlainValue(
      pickConversationImages(messages, pickedGenerations, imagesById),
    ),
    messagesCursor,
    messagesHasMore,
    updatedAt: now,
  };
}

function suggestedFilename(
  metadata: Record<string, unknown> | null | undefined,
): string | undefined {
  return typeof metadata?.suggested_filename === "string"
    ? metadata.suggested_filename
    : undefined;
}

function undefinedIfNullish<T>(
  value: T | null | undefined,
): T | undefined {
  return value == null ? undefined : value;
}

function firstDefined<T>(
  primary: T | null | undefined,
  secondary: T | null | undefined,
): T | undefined {
  if (primary != null) return primary;
  return secondary == null ? undefined : secondary;
}

function firstDefinedOrNull<T>(
  primary: T | null | undefined,
  secondary: T | null | undefined,
): T | null {
  return firstDefined(primary, secondary) ?? null;
}

function stringWithFallback(
  value: unknown,
  fallback: string,
): string {
  return typeof value === "string" ? value : fallback;
}

function finiteNumberWithFallback(
  value: unknown,
  fallback: number,
): number {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : fallback;
}

function isoTimestampOrUndefined(
  value: string | null | undefined,
): number | undefined {
  return value ? isoToMs(value) : undefined;
}

function preferredImageDataUrl(
  existingDataUrl: string,
  incomingDataUrl: string,
): string {
  if (isEvictableDataUrl(existingDataUrl)) return existingDataUrl;
  return existingDataUrl || incomingDataUrl;
}

function backendImageGenerationId(image: BackendImageMeta): string {
  const completionId =
    typeof image.metadata_jsonb?.completion_id === "string"
      ? image.metadata_jsonb.completion_id
      : "";
  return (
    image.owner_generation_id ??
    (completionId ? completionToolGenerationId(completionId) : "")
  );
}

function mergeExistingImage(
  existing: GeneratedImage,
  image: BackendImageMeta,
): GeneratedImage {
  const billingMeta = billingMetaFromPayload(image, image.metadata_jsonb);
  return {
    ...existing,
    data_url: preferredImageDataUrl(existing.data_url, image.url),
    display_url: firstDefined(existing.display_url, image.display_url),
    preview_url: firstDefined(existing.preview_url, image.preview_url),
    thumb_url: firstDefined(existing.thumb_url, image.thumb_url),
    mime: firstDefined(existing.mime, image.mime),
    filename: firstDefined(
      existing.filename,
      suggestedFilename(image.metadata_jsonb),
    ),
    metadata_jsonb: firstDefinedOrNull(
      existing.metadata_jsonb,
      image.metadata_jsonb,
    ),
    is_dual_race_bonus: firstDefined(
      existing.is_dual_race_bonus,
      billingMeta.is_dual_race_bonus,
    ),
    billing_free: firstDefined(
      existing.billing_free,
      billingMeta.billing_free,
    ),
    billing_label: firstDefined(
      existing.billing_label,
      billingMeta.billing_label,
    ),
    billing_exempt_reason: firstDefined(
      existing.billing_exempt_reason,
      billingMeta.billing_exempt_reason,
    ),
  };
}

function buildBackendImage(image: BackendImageMeta): GeneratedImage {
  const sizeActual = `${image.width}x${image.height}`;
  return {
    id: image.id,
    data_url: image.url,
    mime: image.mime ?? undefined,
    display_url: image.display_url ?? undefined,
    preview_url: image.preview_url ?? undefined,
    thumb_url: image.thumb_url ?? undefined,
    width: image.width,
    height: image.height,
    parent_image_id: image.parent_image_id,
    from_generation_id: backendImageGenerationId(image),
    size_requested: sizeActual,
    size_actual: sizeActual,
    filename: suggestedFilename(image.metadata_jsonb),
    metadata_jsonb: image.metadata_jsonb ?? null,
    ...billingMetaFromPayload(image, image.metadata_jsonb),
  };
}

function materializeImages(
  images: BackendImageMeta[] | null | undefined,
  existingImages: Record<string, GeneratedImage>,
): {
  imagesById: Record<string, GeneratedImage>;
  imageIds: Set<string>;
} {
  const imagesById = { ...existingImages };
  const imageIds = new Set<string>();
  for (const image of images ?? []) {
    const existing = imagesById[image.id];
    imagesById[image.id] = existing
      ? mergeExistingImage(existing, image)
      : buildBackendImage(image);
    imageIds.add(image.id);
  }
  return { imagesById, imageIds };
}

function firstImageByGenerationId(
  images: BackendImageMeta[] | null | undefined,
): Map<string, BackendImageMeta> {
  const byGenerationId = new Map<string, BackendImageMeta>();
  for (const image of images ?? []) {
    const generationId = image.owner_generation_id;
    if (generationId && !byGenerationId.has(generationId)) {
      byGenerationId.set(generationId, image);
    }
  }
  return byGenerationId;
}

function appendGenerationId(
  idsByMessage: Record<string, string[]>,
  messageId: string,
  generationId: string,
): void {
  const current = idsByMessage[messageId] ?? [];
  if (!current.includes(generationId)) {
    idsByMessage[messageId] = [...current, generationId];
  }
}

function buildGenerationSnapshot(
  generation: BackendGeneration,
  existing: Generation | undefined,
  image: GeneratedImage | undefined,
  explainability: GenerationExplainabilityMeta,
): Generation {
  return {
    id: generation.id,
    message_id: generation.message_id,
    parent_generation_id: firstDefinedOrNull(
      generation.parent_generation_id,
      existing?.parent_generation_id,
    ),
    action: generation.action === "edit" ? "edit" : "generate",
    prompt: stringWithFallback(generation.prompt, existing?.prompt ?? ""),
    size_requested: stringWithFallback(
      generation.size_requested,
      existing?.size_requested ?? "auto",
    ),
    aspect_ratio: coerceAspectRatio(
      generation.aspect_ratio,
      existing?.aspect_ratio ?? DEFAULT_PARAMS.aspect_ratio,
    ),
    input_image_ids: stringArray(generation.input_image_ids),
    primary_input_image_id: stringOrNull(generation.primary_input_image_id),
    status: coerceGenerationStatus(
      generation.status,
      existing?.status ?? "succeeded",
    ),
    stage: coerceGenerationStage(
      generation.progress_stage,
      existing?.stage ?? "finalizing",
    ),
    image,
    error_code: undefinedIfNullish(generation.error_code),
    error_message: undefinedIfNullish(generation.error_message),
    attempt: finiteNumberWithFallback(
      generation.attempt,
      existing?.attempt ?? 0,
    ),
    started_at: isoToMs(generation.started_at),
    finished_at: isoTimestampOrUndefined(generation.finished_at),
    ...explainability,
    ...billingMetaFromPayload(generation),
  };
}

function materializeGenerations(
  generations: BackendGeneration[] | null | undefined,
  images: BackendImageMeta[] | null | undefined,
  existingGenerations: Record<string, Generation>,
  imagesById: Record<string, GeneratedImage>,
): {
  generations: Record<string, Generation>;
  generationIdsByMessage: Record<string, string[]>;
  materialized: Generation[];
} {
  const next = { ...existingGenerations };
  const generationIdsByMessage: Record<string, string[]> = {};
  const materialized: Generation[] = [];
  const imageByGenerationId = firstImageByGenerationId(images);

  for (const generation of generations ?? []) {
    const existing = existingGenerations[generation.id];
    const linkedImage = imageByGenerationId.get(generation.id);
    const builtImage = linkedImage
      ? imagesById[linkedImage.id]
      : undefined;
    const explainability = generationExplainabilityFromBackend(generation);
    const image = mergeExplainabilityIntoImage(
      builtImage ?? existing?.image,
      explainability,
    );
    if (image) imagesById[image.id] = image;
    const snapshot = buildGenerationSnapshot(
      generation,
      existing,
      image,
      explainability,
    );
    const preferred = preferredGenerationSnapshot(existing, snapshot);
    next[generation.id] = preferred;
    materialized.push(preferred);
    appendGenerationId(
      generationIdsByMessage,
      generation.message_id,
      generation.id,
    );
  }

  return {
    generations: next,
    generationIdsByMessage,
    materialized,
  };
}

function materializeCompletionIds(
  response: MessageListResponse,
): {
  completionIdsByMessage: Record<string, string>;
  materialized: MessageListMaterialization["completionMessages"];
} {
  const completionIdsByMessage: Record<string, string> = {};
  const materialized: MessageListMaterialization["completionMessages"] = [];
  for (const completion of response.completions ?? []) {
    completionIdsByMessage[completion.message_id] = completion.id;
    materialized.push({
      completionId: completion.id,
      messageId: completion.message_id,
    });
  }
  return { completionIdsByMessage, materialized };
}

function contentImageIds(message: BackendMessage): string[] {
  const images = Array.isArray(message.content?.images)
    ? message.content.images
    : [];
  return images.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const imageId = (item as { image_id?: unknown }).image_id;
    return typeof imageId === "string" && imageId ? [imageId] : [];
  });
}

function buildCompletionToolGeneration(
  message: BackendMessage,
  completionId: string,
  imageIds: string[],
  imagesById: Record<string, GeneratedImage>,
  existingGenerations: Record<string, Generation>,
): Generation {
  const generationId = completionToolGenerationId(completionId);
  const existing = existingGenerations[generationId];
  const firstImage = imageIds
    .map((imageId) => imagesById[imageId])
    .find(Boolean);
  return {
    id: generationId,
    message_id: message.id,
    action: "generate",
    prompt:
      typeof message.content?.text === "string"
        ? message.content.text
        : "",
    size_requested: firstImage?.size_requested ?? "auto",
    aspect_ratio: DEFAULT_PARAMS.aspect_ratio,
    input_image_ids: [],
    primary_input_image_id: null,
    status: "succeeded",
    stage: "finalizing",
    image: firstImage ?? existing?.image,
    attempt: existing?.attempt ?? 0,
    started_at: existing?.started_at ?? isoToMs(message.created_at),
    finished_at: existing?.finished_at ?? isoToMs(message.created_at),
  };
}

function materializeCompletionToolGenerations(
  items: BackendMessage[],
  completionIdsByMessage: Record<string, string>,
  existingGenerations: Record<string, Generation>,
  generations: Record<string, Generation>,
  imagesById: Record<string, GeneratedImage>,
  generationIdsByMessage: Record<string, string[]>,
  imageIds: Set<string>,
): Generation[] {
  const materialized: Generation[] = [];
  for (const message of items) {
    if (message.role !== "assistant") continue;
    const completionId = completionIdsByMessage[message.id];
    if (!completionId) continue;
    const linkedImageIds = contentImageIds(message);
    if (linkedImageIds.length === 0) continue;
    const generation = buildCompletionToolGeneration(
      message,
      completionId,
      linkedImageIds,
      imagesById,
      existingGenerations,
    );
    generations[generation.id] = generation;
    materialized.push(generation);
    generationIdsByMessage[message.id] = [
      ...(generationIdsByMessage[message.id] ?? []),
      generation.id,
    ];
    for (const imageId of linkedImageIds) imageIds.add(imageId);
  }
  return materialized;
}

function appendExistingGenerationIds(
  existingGenerations: Record<string, Generation>,
  generationIdsByMessage: Record<string, string[]>,
): void {
  for (const generation of Object.values(existingGenerations)) {
    if (!generation.message_id) continue;
    appendGenerationId(
      generationIdsByMessage,
      generation.message_id,
      generation.id,
    );
  }
}

function attachmentImages(message: BackendMessage): AttachmentImage[] {
  const attachments = Array.isArray(message.content?.attachments)
    ? message.content.attachments
    : [];
  return attachments.flatMap((attachment): AttachmentImage[] => {
    if (!attachment || typeof attachment !== "object") return [];
    const imageId = (attachment as { image_id?: unknown }).image_id;
    if (typeof imageId !== "string" || !imageId) return [];
    return [
      {
        id: imageId,
        kind: "upload",
        data_url: imageBinaryUrl(imageId),
        mime: "",
      },
    ];
  });
}

function adaptHistoryMessage(
  message: BackendMessage,
  generationIdsByMessage: Record<string, string[]>,
  completionIdsByMessage: Record<string, string>,
  generations: Record<string, Generation>,
): Message | null {
  if (message.role === "user") {
    return adaptBackendUserMessage(
      message,
      attachmentImages(message),
      DEFAULT_PARAMS,
      "auto",
    );
  }
  if (message.role !== "assistant") return null;
  const generationIds = generationIdsByMessage[message.id];
  const assistant = adaptBackendAssistantMessage(
    message,
    "",
    "chat",
    generationIds,
    completionIdsByMessage[message.id],
  );
  return {
    ...assistant,
    status: aggregateGenerationStatus(
      generationIds ?? [],
      generations,
      coerceAssistantStatus(message.status),
    ),
  } satisfies AssistantMessage;
}

function adaptHistoryMessages(
  items: BackendMessage[],
  generationIdsByMessage: Record<string, string[]>,
  completionIdsByMessage: Record<string, string>,
  generations: Record<string, Generation>,
): Message[] {
  return items.flatMap((message): Message[] => {
    const adapted = adaptHistoryMessage(
      message,
      generationIdsByMessage,
      completionIdsByMessage,
      generations,
    );
    return adapted ? [adapted] : [];
  });
}

export function buildMessageListState(
  response: MessageListResponse,
  existingGenerations: Record<string, Generation>,
  existingImages: Record<string, GeneratedImage>,
): BuiltMessageListState {
  const items = response.items ?? [];
  const imageState = materializeImages(response.images, existingImages);
  const generationState = materializeGenerations(
    response.generations,
    response.images,
    existingGenerations,
    imageState.imagesById,
  );
  const completionState = materializeCompletionIds(response);
  const completionGenerations = materializeCompletionToolGenerations(
    items,
    completionState.completionIdsByMessage,
    existingGenerations,
    generationState.generations,
    imageState.imagesById,
    generationState.generationIdsByMessage,
    imageState.imageIds,
  );
  appendExistingGenerationIds(
    existingGenerations,
    generationState.generationIdsByMessage,
  );
  const messages = adaptHistoryMessages(
    items,
    generationState.generationIdsByMessage,
    completionState.completionIdsByMessage,
    generationState.generations,
  );
  return {
    messages,
    generations: generationState.generations,
    imagesById: imageState.imagesById,
    materialization: {
      imageIds: Array.from(imageState.imageIds),
      generations: [
        ...generationState.materialized,
        ...completionGenerations,
      ],
      completionMessages: completionState.materialized,
      messages,
    },
  };
}
