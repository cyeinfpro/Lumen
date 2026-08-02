import {
  completionToolGenerationId,
  generationIdsOfMessage,
} from "@/features/generation";
import type {
  AssistantMessage,
  Generation,
  GeneratedImage,
} from "@/lib/types";
import {
  bufferPendingCompletionImage,
  getPendingCompletionImage,
  pendingCompletionImagesForScope,
  removePendingCompletionImage,
  type PendingCompletionImage,
} from "./completionImageBuffer";
import { DEFAULT_PARAMS } from "./imageParams";
import {
  _imageConvIds,
  completionMessageLookupId,
  generationConversationId,
  invalidateConversationHistoryCache,
  rememberCompletionMessage,
  rememberGenerationForConversation,
  scheduleBase64Eviction,
  setBounded,
} from "./runtime";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
} from "./types";

export interface CompletionImageTarget {
  completionId: string;
  rawMessageId?: string;
}

type CompletionImageOwnerResolution =
  | { kind: "ready"; messageId: string }
  | { kind: "pending" }
  | { kind: "conflict" };

type DirectMessageOwner =
  | { kind: "absent" }
  | { kind: "owner"; messageId: string }
  | { kind: "conflict" };

function userScopeOf(state: Pick<ChatState, "currentUserId">): string | null {
  return state.currentUserId ? `user:${state.currentUserId}` : null;
}

function completionMessageOwner(
  state: ChatState,
  completionId: string,
): string | undefined {
  return state.messages.find(
    (message): message is AssistantMessage =>
      message.role === "assistant" &&
      message.completion_id === completionId,
  )?.id;
}

function directMessageOwner(
  state: ChatState,
  target: CompletionImageTarget,
): DirectMessageOwner {
  if (!target.rawMessageId) return { kind: "absent" };
  const message = state.messages.find(
    (candidate): candidate is AssistantMessage =>
      candidate.role === "assistant" &&
      candidate.id === target.rawMessageId,
  );
  if (!message) return { kind: "absent" };
  if (
    message.completion_id &&
    message.completion_id !== target.completionId &&
    !message.completion_id.startsWith("opt-")
  ) {
    return { kind: "conflict" };
  }
  return { kind: "owner", messageId: message.id };
}

function resolveCompletionImageOwner(
  owners: Set<string>,
  rawMessageId: string | undefined,
  hasMaterializedOwner: boolean,
): CompletionImageOwnerResolution {
  if (owners.size > 1) return { kind: "conflict" };
  const messageId = owners.values().next().value;
  if (!messageId) return { kind: "pending" };
  if (rawMessageId && rawMessageId !== messageId) {
    return { kind: "conflict" };
  }
  return hasMaterializedOwner
    ? { kind: "ready", messageId }
    : { kind: "pending" };
}

function completionImageOwner(
  state: ChatState,
  target: CompletionImageTarget,
  generationId: string,
  now: number,
): CompletionImageOwnerResolution {
  if (!state.currentUserId) return { kind: "conflict" };
  const existingGeneration = state.generations[generationId];
  const knownMessageId = completionMessageLookupId(target.completionId, now);
  const completionMessageId = completionMessageOwner(
    state,
    target.completionId,
  );
  const directOwner = directMessageOwner(state, target);
  if (directOwner.kind === "conflict") {
    return { kind: "conflict" };
  }
  const directMessageId =
    directOwner.kind === "owner" ? directOwner.messageId : undefined;
  const owners = new Set(
    [
      existingGeneration?.message_id,
      knownMessageId,
      completionMessageId,
      directMessageId,
    ].filter((messageId): messageId is string => Boolean(messageId)),
  );
  return resolveCompletionImageOwner(
    owners,
    target.rawMessageId,
    Boolean(
      existingGeneration ||
        completionMessageId ||
        directMessageId,
    ),
  );
}

function completionImageState(
  state: ChatState,
  messageId: string,
  generationId: string,
  image: GeneratedImage,
  eventNow: number,
): Partial<ChatState> {
  const existingGeneration = state.generations[generationId];
  const baseGeneration: Generation = existingGeneration ?? {
    id: generationId,
    message_id: messageId,
    action: "generate",
    prompt: "",
    size_requested: image.size_requested,
    aspect_ratio: DEFAULT_PARAMS.aspect_ratio,
    input_image_ids: [],
    primary_input_image_id: null,
    status: "succeeded",
    stage: "finalizing",
    attempt: 0,
    started_at: eventNow,
  };
  const nextGeneration: Generation = {
    ...baseGeneration,
    image,
    status: "succeeded",
    stage: "finalizing",
    finished_at: eventNow,
  };
  const convId = generationConversationId(state, nextGeneration);
  if (convId) {
    rememberGenerationForConversation(convId, nextGeneration);
    setBounded(_imageConvIds, image.id, convId);
    invalidateConversationHistoryCache(convId);
  }
  return {
    messages: state.messages.map((message) => {
      if (message.role !== "assistant" || message.id !== messageId) {
        return message;
      }
      const existingIds = generationIdsOfMessage(message);
      return {
        ...message,
        status: "streaming",
        generation_ids: existingIds.includes(generationId)
          ? existingIds
          : [...existingIds, generationId],
        generation_id: message.generation_id ?? generationId,
        last_delta_at: eventNow,
      } as AssistantMessage;
    }),
    generations: {
      ...state.generations,
      [generationId]: nextGeneration,
    },
    imagesById: { ...state.imagesById, [image.id]: image },
  };
}

function applyPendingCompletionImage(
  set: ChatStateSetter,
  get: ChatStateGetter,
  entry: PendingCompletionImage,
): "applied" | "pending" | "dropped" {
  const state = get();
  if (userScopeOf(state) !== entry.userScope) return "dropped";
  const generationId = completionToolGenerationId(entry.completionId);
  const target: CompletionImageTarget = {
    completionId: entry.completionId,
    rawMessageId: entry.rawMessageId,
  };
  const initialOwner = completionImageOwner(
    state,
    target,
    generationId,
    Date.now(),
  );
  if (initialOwner.kind === "conflict") return "dropped";
  if (initialOwner.kind === "pending") return "pending";

  let applied = false;
  set((current) => {
    if (userScopeOf(current) !== entry.userScope) return current;
    const currentOwner = completionImageOwner(
      current,
      target,
      generationId,
      Date.now(),
    );
    if (
      currentOwner.kind !== "ready" ||
      currentOwner.messageId !== initialOwner.messageId
    ) {
      return current;
    }
    applied = true;
    return completionImageState(
      current,
      currentOwner.messageId,
      generationId,
      entry.image,
      entry.eventNow,
    );
  });
  if (!applied) return "pending";
  rememberCompletionMessage(
    entry.completionId,
    initialOwner.messageId,
  );
  scheduleBase64Eviction();
  return "applied";
}

export function reconcileCompletionImage(
  set: ChatStateSetter,
  get: ChatStateGetter,
  target: CompletionImageTarget,
  image: GeneratedImage,
  eventNow: number,
): "applied" | "buffered" | "dropped" {
  const userScope = userScopeOf(get());
  if (!userScope) return "dropped";
  bufferPendingCompletionImage({
    userScope,
    completionId: target.completionId,
    rawMessageId: target.rawMessageId,
    image,
    eventNow,
  });
  const result = drainPendingCompletionImage(set, get, target.completionId);
  return result === "pending" ? "buffered" : result;
}

export function drainPendingCompletionImage(
  set: ChatStateSetter,
  get: ChatStateGetter,
  completionId: string,
): "applied" | "pending" | "dropped" {
  const userScope = userScopeOf(get());
  if (!userScope) return "dropped";
  const entry = getPendingCompletionImage(userScope, completionId);
  if (!entry) return "pending";
  const result = applyPendingCompletionImage(set, get, entry);
  if (result !== "pending") {
    removePendingCompletionImage(userScope, completionId);
  }
  return result;
}

export function drainReadyPendingCompletionImages(
  set: ChatStateSetter,
  get: ChatStateGetter,
): void {
  const userScope = userScopeOf(get());
  if (!userScope) return;
  for (const entry of pendingCompletionImagesForScope(userScope)) {
    drainPendingCompletionImage(set, get, entry.completionId);
  }
}
