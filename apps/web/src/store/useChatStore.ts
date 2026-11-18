"use client";

// Lumen 会话 store（后端接入版）
// 对齐 DESIGN.md §13.1 / §22.9（消息 → 任务 → 图像 三层状态机）。
// 乐观插入用户 msg + pending 助手 msg → POST /conversations/:id/messages →
// 用返回的 user_message / assistant_message / generation_ids 校正 → SSE 流式更新。
//
// 本文件不直接调用上游网关；所有网络交互走 apiClient。

import { create } from "zustand";
import { logWarn } from "@/lib/logger";
import { setMaxUploadSourceBytes } from "@/lib/uploadLimits";
import {
  createComposerActions,
  createComposerState,
} from "./chat/composerSlice";
import { applyCompletionEventToMessage } from "./chat/completionEvents";
import { createConversationActions } from "./chat/conversationActions";
import { assistantHasGeneration } from "@/features/generation";
import { createGenerationActions } from "./chat/generationActions";
import { buildMessageListState } from "./chat/history";
import { normalizeImageParams } from "./chat/imageParams";
import {
  CONVERSATION_HISTORY_MEDIA_CACHE_LIMIT,
  MAX_RETAINED_TERMINAL_MEDIA_ITEMS,
  buildTerminalMediaLruEvictionPatch,
  clearMediaLruTracking,
  dropRecentConversationMediaRefs,
  rememberRecentConversationMedia,
  syncChangedMediaKeysToLru,
  touchMediaRecordKeys,
} from "./chat/mediaLru";
import { adaptBackendAssistantMessage } from "./chat/messageAdapters";
import { mergeMessagesById } from "./chat/messageReconciliation";
import { structuredAttachmentsFromComposer } from "./chat/payload";
import { createSendMessageAction } from "./chat/sendMessageAction";
import {
  applySseEventPayload,
  clearAppendedMessageRecoveryQueue,
} from "./chat/sseEventActions";
import { createTaskRecoveryActions } from "./chat/taskRecovery";
import {
  _conversationMutationFence,
  _generationConvIds,
  _imageConvIds,
  _messageConvIds,
  _userSessionFence,
  abortAllHistoryRequests,
  abortAllSendRequests,
  bindChatStoreRuntime,
  clearCompletionStreamBuffer,
  clearConversationIndexes,
  clearUserScopedRuntime,
  disposeChatRuntime,
  errorToMessage,
  flushCompletionStreamPatches,
  generationConversationId,
  invalidateConversationHistoryCache,
  isAbortRequest,
  readConversationHistoryCache,
  rememberCompletionMessage,
  rememberGenerationForConversation,
  scheduleBase64Eviction,
  setBounded,
  ssePayloadRecord,
} from "./chat/runtime";
import type {
  ChatDataSlice,
  ChatState,
  ChatStateSetter,
  ComposerState,
} from "./chat/types";

// Delegated action modules retain normalization from "./chat/imageParams",
// payload decoding from "./chat/payload", adapters from "./chat/messageAdapters",
// completion reducers from "./chat/completionEvents", reconciliation from
// "./chat/messageReconciliation", generation reducers from
// generation model, and history materialization from "./chat/history".
// The send reconciliation contract still calls:
// rememberCompletionMessage(completionId, realAssistant.id);

export type { ReasoningEffort } from "./chat/types";
export {
  CONVERSATION_HISTORY_MEDIA_CACHE_LIMIT,
  MAX_RETAINED_TERMINAL_MEDIA_ITEMS,
  buildTerminalMediaLruEvictionPatch,
};

let _runtimeFastDefault: boolean | null = null;
let _fastTouchedByUser = false;

const CHAT_FACADE_DELEGATES = Object.freeze({
  normalizeImageParams,
  structuredAttachmentsFromComposer,
  adaptBackendAssistantMessage,
  applyCompletionEventToMessage,
  assistantHasGeneration,
  buildMessageListState,
  mergeMessagesById,
});

function createInitialComposer(): ComposerState {
  return createComposerState(_runtimeFastDefault);
}

function createInitialChatData(): ChatDataSlice {
  return {
    currentUserId: null,
    currentConvId: null,
    messages: [],
    generations: {},
    imagesById: {},
    messagesCursor: null,
    messagesHasMore: false,
    messagesLoading: false,
    messagesError: null,
    composerError: null,
    composer: createInitialComposer(),
    composerExpanded: false,
  };
}

function appendMessage(
  messages: ChatState["messages"],
  message: ChatState["messages"][number],
): ChatState["messages"] {
  if (!messages.some((existing) => existing.id === message.id)) {
    return [...messages, message];
  }
  return mergeMessagesById(messages, [message]);
}

function createChatStore() {
  return create<ChatState>((rawSet, get) => {
    const set: ChatStateSetter = (partial) => {
      const prev = get();
      rawSet(partial);
      const next = get();
      syncChangedMediaKeysToLru(
        prev.generations,
        next.generations,
        prev.imagesById,
        next.imagesById,
      );
      dropRecentConversationMediaRefs(next.currentConvId);
    };
    const sendMessageAction = createSendMessageAction(set, get, {
      createInitialComposer,
      facadeDelegates: CHAT_FACADE_DELEGATES,
    });
    return {
      ...createInitialChatData(),
      setCurrentUser: (id) => {
        const previousUserId = get().currentUserId;
        if (previousUserId === id) return;
        if (previousUserId === null && id !== null) {
          set({ currentUserId: id });
          return;
        }
        _userSessionFence.advance();
        _conversationMutationFence.advance();
        clearAppendedMessageRecoveryQueue();
        clearUserScopedRuntime();
        clearMediaLruTracking();
        set({ ...createInitialChatData(), currentUserId: id });
      },
      applyRuntimeDefaults: (defaults) => {
        setMaxUploadSourceBytes(defaults.upload_max_source_bytes);
        if (typeof defaults.fast !== "boolean") return;
        const fastDefault = defaults.fast;
        _runtimeFastDefault = fastDefault;
        set((state) => {
          if (_fastTouchedByUser || state.composer.fast === fastDefault) {
            return state;
          }
          return {
            composer: { ...state.composer, fast: fastDefault },
          };
        });
      },
      // 切换会话只清当前消息；全局 generation / image 池继续承接后台任务，并对超上限终态非活跃条目按 LRU 回收。
      setCurrentConv: (id) => {
        const previousState = get();
        const previousConvId = previousState.currentConvId;
        if (previousConvId === id) return;
        rememberRecentConversationMedia(
          previousConvId,
          previousState.messages,
          previousState.generations,
          previousState.imagesById,
          true,
        );
        dropRecentConversationMediaRefs(id);
        _conversationMutationFence.advance();
        const cached = readConversationHistoryCache(id);
        if (cached) {
          touchMediaRecordKeys(cached.generations, cached.imagesById);
        }
        abortAllHistoryRequests();
        abortAllSendRequests();
        set((state) => {
          const nextMessages = cached?.messages ?? [];
          const mergedGenerations = cached
            ? { ...state.generations, ...cached.generations }
            : state.generations;
          const mergedImagesById = cached
            ? { ...state.imagesById, ...cached.imagesById }
            : state.imagesById;
          const gcPatch = buildTerminalMediaLruEvictionPatch({
            currentConvId: id,
            messages: nextMessages,
            generations: mergedGenerations,
            imagesById: mergedImagesById,
          });
          return {
            currentConvId: id,
            messages: nextMessages,
            generations: gcPatch ? gcPatch.generations : mergedGenerations,
            imagesById: gcPatch ? gcPatch.imagesById : mergedImagesById,
            messagesCursor: cached?.messagesCursor ?? null,
            messagesHasMore: cached?.messagesHasMore ?? false,
            messagesLoading: false,
            messagesError: null,
          };
        });
        clearCompletionStreamBuffer();
        scheduleBase64Eviction();
      },

      ...createComposerActions(set, get, {
        createInitialComposer,
        markFastTouched: () => {
          _fastTouchedByUser = true;
        },
      }),

      ...createConversationActions(set, get),

      async sendMessage(opts) {
        await sendMessageAction(opts);
      },

      ...createGenerationActions(set, get),

      appendUserMessage: (message) => {
        const convId = get().currentConvId;
        if (convId) setBounded(_messageConvIds, message.id, convId);
        invalidateConversationHistoryCache(convId);
        set((state) => ({
          messages: appendMessage(state.messages, message),
        }));
      },
      appendAssistantMessage: (message) => {
        const convId = get().currentConvId;
        if (convId) setBounded(_messageConvIds, message.id, convId);
        rememberCompletionMessage(message.completion_id, message.id);
        invalidateConversationHistoryCache(convId);
        set((state) => ({
          messages: appendMessage(state.messages, message),
        }));
      },
      upsertGeneration: (generation) => {
        const convId =
          generation.conversation_id ??
          _generationConvIds.get(generation.id) ??
          _messageConvIds.get(generation.message_id) ??
          get().currentConvId;
        if (convId) rememberGenerationForConversation(convId, generation);
        invalidateConversationHistoryCache(convId);
        set((state) => ({
          generations: {
            ...state.generations,
            [generation.id]: generation,
          },
        }));
      },
      attachImageToGeneration: (generationId, image) => {
        const finishedAt = Date.now();
        set((state) => {
          const generation = state.generations[generationId];
          if (!generation) return state;
          const convId = generationConversationId(state, generation);
          if (convId) {
            setBounded(_imageConvIds, image.id, convId);
            invalidateConversationHistoryCache(convId);
          }
          return {
            generations: {
              ...state.generations,
              [generationId]: {
                ...generation,
                image,
                status: "succeeded",
                stage: "finalizing",
                finished_at: finishedAt,
              },
            },
            imagesById: {
              ...state.imagesById,
              [image.id]: image,
            },
          };
        });
      },

      // 对齐 DESIGN §5.7；未知事件静默忽略，事件族归并在专用模块完成。
      applySSEEvent(eventName, data, cursor) {
        const eventNow = Date.now();
        const payload = ssePayloadRecord(eventName, data);
        if (!payload) return;
        invalidateConversationHistoryCache(get().currentConvId);
        try {
          applySseEventPayload(
            set,
            get,
            eventName,
            payload,
            eventNow,
            CHAT_FACADE_DELEGATES,
            cursor,
          );
        } catch (err) {
          logWarn("dropped SSE event after store handler error", {
            scope: "chat-sse",
            extra: { event: eventName, err: errorToMessage(err) },
          });
        }
      },

      ...createTaskRecoveryActions(set, get, {
        flushCompletionStreamPatches,
        userSessionFence: _userSessionFence,
        isAbortRequest,
        errorToMessage,
      }),

      reset: () => {
        _runtimeFastDefault = null;
        _fastTouchedByUser = false;
        _userSessionFence.advance();
        _conversationMutationFence.advance();
        clearAppendedMessageRecoveryQueue();
        clearUserScopedRuntime();
        clearMediaLruTracking();
        if (typeof window !== "undefined") {
          window.dispatchEvent(new Event("lumen:chat-store-reset"));
        }
        set(createInitialChatData());
      },
    };
  });
}

type ChatStoreHook = ReturnType<typeof createChatStore>;

let browserChatStore: ChatStoreHook | null = null;

function getChatStore(): ChatStoreHook {
  if (typeof window === "undefined") {
    clearCompletionStreamBuffer();
    clearConversationIndexes();
    clearMediaLruTracking();
    return createChatStore();
  }
  if (!browserChatStore) {
    browserChatStore = createChatStore();
  }
  return browserChatStore;
}

export const useChatStore: ChatStoreHook = new Proxy(
  ((...args: Parameters<ChatStoreHook>) =>
    getChatStore()(...args)) as ChatStoreHook,
  {
    get(_target, prop, receiver) {
      return Reflect.get(getChatStore(), prop, receiver);
    },
    set(_target, prop, value, receiver) {
      return Reflect.set(getChatStore(), prop, value, receiver);
    },
    has(_target, prop) {
      return prop in getChatStore();
    },
    ownKeys() {
      return Reflect.ownKeys(getChatStore());
    },
    getOwnPropertyDescriptor(_target, prop) {
      return Reflect.getOwnPropertyDescriptor(getChatStore(), prop);
    },
  },
) as ChatStoreHook;

export function runTerminalMediaLruGc(
  maxRetained = MAX_RETAINED_TERMINAL_MEDIA_ITEMS,
): boolean {
  const current = useChatStore.getState();
  const patch = buildTerminalMediaLruEvictionPatch(current, maxRetained);
  if (!patch) return false;
  useChatStore.setState(patch);
  return true;
}

bindChatStoreRuntime({
  getState: () => useChatStore.getState(),
  setState: (partial) => {
    useChatStore.setState(partial);
    runTerminalMediaLruGc();
  },
});

export function disposeChatStoreRuntime(): void {
  clearMediaLruTracking();
  disposeChatRuntime();
}

const hot = (
  import.meta as ImportMeta & { hot?: { dispose: (cb: () => void) => void } }
).hot;
if (hot) {
  hot.dispose(disposeChatStoreRuntime);
}
