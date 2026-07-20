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
import { createGenerationActions } from "./chat/generationActions";
import { assistantHasGeneration } from "./chat/generationSlice";
import { buildMessageListState } from "./chat/history";
import { normalizeImageParams } from "./chat/imageParams";
import { adaptBackendAssistantMessage } from "./chat/messageAdapters";
import { mergeMessagesById } from "./chat/messageReconciliation";
import { structuredAttachmentsFromComposer } from "./chat/payload";
import { createSendMessageAction } from "./chat/sendMessageAction";
import { applySseEventPayload } from "./chat/sseEventActions";
import { createTaskRecoveryActions } from "./chat/taskRecovery";
import {
  _conversationMutationFence,
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
  ComposerState,
} from "./chat/types";

// Delegated action modules retain normalization from "./chat/imageParams",
// payload decoding from "./chat/payload", adapters from "./chat/messageAdapters",
// completion reducers from "./chat/completionEvents", reconciliation from
// "./chat/messageReconciliation", generation reducers from
// "./chat/generationSlice", and history materialization from "./chat/history".
// The send reconciliation contract still calls:
// rememberCompletionMessage(completionId, realAssistant.id);

export type { ReasoningEffort } from "./chat/types";

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
  return create<ChatState>((set, get) => {
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
        clearUserScopedRuntime();
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
      // 切换会话只清当前消息；全局 generation / image 池继续承接后台任务。
      setCurrentConv: (id) => {
        const previousConvId = get().currentConvId;
        if (previousConvId === id) return;
        _conversationMutationFence.advance();
        const cached = readConversationHistoryCache(id);
        abortAllHistoryRequests();
        abortAllSendRequests();
        set((state) => ({
          currentConvId: id,
          messages: cached?.messages ?? [],
          generations: cached
            ? { ...state.generations, ...cached.generations }
            : state.generations,
          imagesById: cached
            ? { ...state.imagesById, ...cached.imagesById }
            : state.imagesById,
          messagesCursor: cached?.messagesCursor ?? null,
          messagesHasMore: cached?.messagesHasMore ?? false,
          messagesLoading: false,
          messagesError: null,
        }));
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

      ...createGenerationActions(set, get, {
        runtimeFastDefault: () => _runtimeFastDefault,
      }),

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
          _messageConvIds.get(generation.message_id) ?? get().currentConvId;
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
      applySSEEvent(eventName, data) {
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
        clearUserScopedRuntime();
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

bindChatStoreRuntime({
  getState: () => useChatStore.getState(),
  setState: (partial) => useChatStore.setState(partial),
});

export function disposeChatStoreRuntime(): void {
  disposeChatRuntime();
}

const hot = (
  import.meta as ImportMeta & { hot?: { dispose: (cb: () => void) => void } }
).hot;
if (hot) {
  hot.dispose(disposeChatStoreRuntime);
}
