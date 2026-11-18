import {
  _generationConvIds,
  _imageConvIds,
  _messageConvIds,
  generationConversationId,
  setBounded,
} from "./runtime";
import type {
  GeneratedImage,
  Generation,
  Message,
} from "@/lib/types";
import type { ChatState } from "./types";

export const MAX_RETAINED_TERMINAL_MEDIA_ITEMS = 1_000;
export const CONVERSATION_HISTORY_MEDIA_CACHE_LIMIT = 32;

const TERMINAL_MEDIA_STATUSES = new Set<string>([
  "succeeded",
  "failed",
  "canceled",
  "cancelled",
]);

interface ConversationMediaRefs {
  generationIds: Set<string>;
  imageIds: Set<string>;
}

const _recentConversationRefs = new Map<string, ConversationMediaRefs>();
const _generationLruOrder = new Map<string, number>();
const _imageLruOrder = new Map<string, number>();
let _mediaLruClock = 0;

export function clearMediaLruTracking(): void {
  _recentConversationRefs.clear();
  _generationLruOrder.clear();
  _imageLruOrder.clear();
  _mediaLruClock = 0;
}

export function dropRecentConversationMediaRefs(
  convId: string | null | undefined,
): void {
  if (!convId) return;
  _recentConversationRefs.delete(convId);
}

function touchLruKey(map: Map<string, number>, id: string): void {
  if (!id) return;
  if (map.has(id)) map.delete(id);
  _mediaLruClock += 1;
  map.set(id, _mediaLruClock);
}

export function touchMediaRecordKeys(
  generations: Record<string, Generation>,
  imagesById: Record<string, GeneratedImage>,
): void {
  for (const id of Object.keys(generations)) {
    touchLruKey(_generationLruOrder, id);
  }
  for (const id of Object.keys(imagesById)) {
    touchLruKey(_imageLruOrder, id);
  }
}

export function syncChangedMediaKeysToLru(
  prevGenerations: Record<string, Generation>,
  nextGenerations: Record<string, Generation>,
  prevImages: Record<string, GeneratedImage>,
  nextImages: Record<string, GeneratedImage>,
): void {
  if (prevGenerations !== nextGenerations) {
    for (const [id, gen] of Object.entries(nextGenerations)) {
      if (prevGenerations[id] !== gen || !_generationLruOrder.has(id)) {
        touchLruKey(_generationLruOrder, id);
        if (gen.image?.id) touchLruKey(_imageLruOrder, gen.image.id);
      }
    }
  }
  if (prevImages !== nextImages) {
    for (const [id, img] of Object.entries(nextImages)) {
      if (prevImages[id] !== img || !_imageLruOrder.has(id)) {
        touchLruKey(_imageLruOrder, id);
      }
    }
  }
}

function addGenerationLinkedImageIds(
  generation: Generation,
  target: Set<string>,
): void {
  if (generation.image?.id) target.add(generation.image.id);
  if (generation.primary_input_image_id) {
    target.add(generation.primary_input_image_id);
  }
  if (generation.mask_image_id) target.add(generation.mask_image_id);
  for (const inputId of generation.input_image_ids) {
    if (inputId) target.add(inputId);
  }
}

function addSingleMessageMediaRefs(
  message: Message,
  generationIds: Set<string>,
  imageIds: Set<string>,
): void {
  if (message.role === "user") {
    for (const attachment of message.attachments) {
      if (attachment.id) imageIds.add(attachment.id);
      if (attachment.source_image_id) imageIds.add(attachment.source_image_id);
    }
    return;
  }
  if (message.generation_id) generationIds.add(message.generation_id);
  for (const id of message.generation_ids ?? []) {
    if (id) generationIds.add(id);
  }
  if (message.completion_id && (message.tool_calls?.length ?? 0) > 0) {
    generationIds.add(`completion-tool-${message.completion_id}`);
  }
  if (message.artifact_image_id) imageIds.add(message.artifact_image_id);
}

function collectMessagesMediaRefs(
  messages: readonly Message[],
): ConversationMediaRefs {
  const generationIds = new Set<string>();
  const imageIds = new Set<string>();
  for (const message of messages) {
    addSingleMessageMediaRefs(message, generationIds, imageIds);
  }
  return { generationIds, imageIds };
}

function resolveGenerationConvId(
  state: Pick<ChatState, "currentConvId" | "messages">,
  generation: Generation,
): string | null {
  return (
    generation.conversation_id ??
    _generationConvIds.get(generation.id) ??
    generationConversationId(state, generation)
  );
}

export function rememberRecentConversationMedia(
  convId: string | null | undefined,
  messages: readonly Message[],
  generations: Record<string, Generation>,
  imagesById: Record<string, GeneratedImage>,
  touchOrder = false,
): void {
  if (!convId) return;
  const previous = _recentConversationRefs.get(convId);
  const refs = collectMessagesMediaRefs(messages);
  if (previous) {
    for (const id of previous.generationIds) refs.generationIds.add(id);
    for (const id of previous.imageIds) refs.imageIds.add(id);
  }
  for (const [id, gen] of Object.entries(generations)) {
    const ownerConv =
      gen.conversation_id ??
      _generationConvIds.get(id) ??
      _messageConvIds.get(gen.message_id);
    if (ownerConv === convId || refs.generationIds.has(id)) {
      refs.generationIds.add(id);
      addGenerationLinkedImageIds(gen, refs.imageIds);
    }
  }
  for (const imageId of Object.keys(imagesById)) {
    if (_imageConvIds.get(imageId) === convId) refs.imageIds.add(imageId);
  }
  if (touchOrder || !_recentConversationRefs.has(convId)) {
    setBounded(
      _recentConversationRefs,
      convId,
      refs,
      CONVERSATION_HISTORY_MEDIA_CACHE_LIMIT,
    );
  } else {
    _recentConversationRefs.set(convId, refs);
  }
}

function isNonTerminalGeneration(generation: Generation | undefined): boolean {
  if (!generation) return false;
  return !TERMINAL_MEDIA_STATUSES.has(generation.status);
}

function mergeMediaRefsInto(
  source: ConversationMediaRefs,
  target: ConversationMediaRefs,
): void {
  for (const id of source.generationIds) target.generationIds.add(id);
  for (const id of source.imageIds) target.imageIds.add(id);
}

function isProtectedGenerationEntry(
  state: Pick<ChatState, "currentConvId" | "messages">,
  id: string,
  gen: Generation,
  activeConvIds: ReadonlySet<string>,
  protectedGenIds: ReadonlySet<string>,
): boolean {
  if (isNonTerminalGeneration(gen) || protectedGenIds.has(id)) return true;
  const convId = resolveGenerationConvId(state, gen);
  return convId !== null && activeConvIds.has(convId);
}

function isProtectedImageEntry(
  id: string,
  image: GeneratedImage,
  activeConvIds: ReadonlySet<string>,
  protectedGenIds: ReadonlySet<string>,
): boolean {
  const imageConv = _imageConvIds.get(id) ?? null;
  if (imageConv !== null && activeConvIds.has(imageConv)) return true;
  return Boolean(
    image.from_generation_id && protectedGenIds.has(image.from_generation_id),
  );
}

function collectProtectedMediaSets(
  state: Pick<
    ChatState,
    "currentConvId" | "messages" | "generations" | "imagesById"
  >,
): ConversationMediaRefs {
  const result: ConversationMediaRefs = {
    generationIds: new Set<string>(),
    imageIds: new Set<string>(),
  };
  const activeConvIds = new Set<string>(_recentConversationRefs.keys());
  if (state.currentConvId) activeConvIds.add(state.currentConvId);

  mergeMediaRefsInto(collectMessagesMediaRefs(state.messages), result);
  for (const refs of _recentConversationRefs.values()) {
    mergeMediaRefsInto(refs, result);
  }

  for (const [id, gen] of Object.entries(state.generations)) {
    if (
      isProtectedGenerationEntry(
        state,
        id,
        gen,
        activeConvIds,
        result.generationIds,
      )
    ) {
      result.generationIds.add(id);
      addGenerationLinkedImageIds(gen, result.imageIds);
    }
  }

  for (const [id, image] of Object.entries(state.imagesById)) {
    if (isProtectedImageEntry(id, image, activeConvIds, result.generationIds)) {
      result.imageIds.add(id);
    }
  }

  return result;
}

function generationLruRank(id: string, generation: Generation): number {
  return (
    _generationLruOrder.get(id) ??
    generation.finished_at ??
    generation.created_at ??
    generation.started_at ??
    0
  );
}

function imageLruRank(
  id: string,
  img: GeneratedImage,
  evictedGenIds: ReadonlySet<string>,
): number {
  const baseOrder = _imageLruOrder.get(id) ?? 0;
  const ownerEvicted = Boolean(
    img.from_generation_id && evictedGenIds.has(img.from_generation_id),
  );
  return ownerEvicted ? -1_000_000_000 + baseOrder : baseOrder;
}

function selectOldestKeysToEvict<T>(
  candidates: Array<[string, T, number]>,
  maxRetained: number,
): Set<string> {
  const safeLimit = Math.max(0, maxRetained);
  if (candidates.length <= safeLimit) return new Set();
  candidates.sort((left, right) => left[2] - right[2]);
  const evictCount = candidates.length - safeLimit;
  const evicted = new Set<string>();
  for (let i = 0; i < evictCount; i += 1) {
    evicted.add(candidates[i]![0]);
  }
  return evicted;
}

function rebuildRecordWithoutEvicted<T>(
  record: Record<string, T>,
  evictedIds: ReadonlySet<string>,
  lruMap: Map<string, number>,
): Record<string, T> {
  if (evictedIds.size === 0) return record;
  const next: Record<string, T> = {};
  for (const [id, item] of Object.entries(record)) {
    if (evictedIds.has(id)) {
      lruMap.delete(id);
    } else {
      next[id] = item;
    }
  }
  return next;
}

export function buildTerminalMediaLruEvictionPatch(
  state: Pick<
    ChatState,
    "currentConvId" | "messages" | "generations" | "imagesById"
  >,
  maxRetained = MAX_RETAINED_TERMINAL_MEDIA_ITEMS,
): Pick<ChatState, "generations" | "imagesById"> | null {
  const protectedSets = collectProtectedMediaSets(state);

  const terminalGenCandidates: Array<[string, Generation, number]> = [];
  for (const [id, gen] of Object.entries(state.generations)) {
    if (!protectedSets.generationIds.has(id)) {
      terminalGenCandidates.push([id, gen, generationLruRank(id, gen)]);
    }
  }
  const evictedGenIds = selectOldestKeysToEvict(
    terminalGenCandidates,
    maxRetained,
  );

  const terminalImageCandidates: Array<[string, GeneratedImage, number]> = [];
  for (const [id, img] of Object.entries(state.imagesById)) {
    if (!protectedSets.imageIds.has(id)) {
      terminalImageCandidates.push([
        id,
        img,
        imageLruRank(id, img, evictedGenIds),
      ]);
    }
  }
  const evictedImageIds = selectOldestKeysToEvict(
    terminalImageCandidates,
    maxRetained,
  );

  if (evictedGenIds.size === 0 && evictedImageIds.size === 0) {
    return null;
  }

  return {
    generations: rebuildRecordWithoutEvicted(
      state.generations,
      evictedGenIds,
      _generationLruOrder,
    ),
    imagesById: rebuildRecordWithoutEvicted(
      state.imagesById,
      evictedImageIds,
      _imageLruOrder,
    ),
  };
}
