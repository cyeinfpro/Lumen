import {
  MAX_UPLOAD_SOURCE_BYTES,
  maxUploadSourceMessage,
} from "@/lib/uploadLimits";
import type { AttachmentImage } from "@/lib/types";
import {
  listMessages as apiListMessages,
} from "@/lib/api/conversations";
import {
  imageBinaryUrl,
  uploadImage as apiUploadImage,
} from "@/lib/api/images";
import { logWarn } from "@/lib/logger";
import { compressToMaxDim } from "./imageUpload";
import {
  buildMessageListState,
  makeConversationHistoryCacheEntry,
  type ConversationHistoryCacheEntry,
} from "./history";
import { mergeMessagesById } from "./messageReconciliation";
import type { ChatState, ChatStateGetter, ChatStateSetter } from "./types";
import {
  errorToMessage,
  isHistoryRequestAbort,
  shouldSkipHistoryLoad,
  MESSAGE_PAGE_LIMIT,
  readConversationHistoryCache,
  rememberConversationHistoryCache,
  rememberMessageListMaterialization,
  abortHistoryRequest,
  _historyAborts,
} from "./runtime";

type HistoryResponse = Awaited<ReturnType<typeof apiListMessages>>;

function primeHistoryLoad(
  set: ChatStateSetter,
  convId: string,
  loadMore: boolean,
  cached: ConversationHistoryCacheEntry | null,
): void {
  set((s) => ({
    messagesLoading: true,
    messagesError: null,
    ...(loadMore
      ? {}
      : cached && s.currentConvId === convId
        ? {
            messages: cached.messages,
            generations: { ...s.generations, ...cached.generations },
            imagesById: { ...s.imagesById, ...cached.imagesById },
            messagesCursor: cached.messagesCursor,
            messagesHasMore: cached.messagesHasMore,
          }
        : { messagesCursor: null, messagesHasMore: false }),
  }));
}

async function fetchHistoryPage(
  convId: string,
  cursor: string | null,
  loadMore: boolean,
  signal: AbortSignal,
  get: ChatStateGetter,
): Promise<HistoryResponse> {
  let response = await apiListMessages(convId, {
    limit: MESSAGE_PAGE_LIMIT,
    cursor: cursor ?? undefined,
    signal,
    include: ["tasks"],
  });

  if (!(loadMore && cursor)) return response;

  const existingIds = new Set(get().messages.map((message) => message.id));
  const newCount = (response.items ?? []).filter(
    (message) => !existingIds.has(message.id),
  ).length;
  if (newCount !== 0 || response.next_cursor !== cursor) return response;

  response = await apiListMessages(convId, {
    limit: MESSAGE_PAGE_LIMIT,
    since: cursor,
    signal,
    include: ["tasks"],
  });
  return response;
}

function commitHistoryPage(
  set: ChatStateSetter,
  convId: string,
  loadMore: boolean,
  response: HistoryResponse,
  built: ReturnType<typeof buildMessageListState>,
): ConversationHistoryCacheEntry | null {
  let cacheEntry: ConversationHistoryCacheEntry | null = null;
  set((s) => {
    if (s.currentConvId !== convId) {
      logWarn(
        "loadHistoricalMessages: conv switched mid-flight; dropping stale result",
        {
          scope: "chat",
          extra: { requested: convId, current: s.currentConvId },
        },
      );
      return s;
    }
    const nextMessages = mergeMessagesById(s.messages, built.messages);
    const nextCursor = response.next_cursor ?? null;
    const gotNewMessages =
      nextMessages.length > s.messages.length || !loadMore;
    const messagesHasMore = Boolean(nextCursor) && gotNewMessages;
    cacheEntry = makeConversationHistoryCacheEntry(
      nextMessages,
      built.generations,
      built.imagesById,
      nextCursor,
      messagesHasMore,
    );
    return {
      messages: nextMessages,
      generations: built.generations,
      imagesById: built.imagesById,
      messagesCursor: nextCursor,
      messagesHasMore,
      messagesLoading: false,
      messagesError: null,
    };
  });
  return cacheEntry;
}

function handleHistoryError(
  set: ChatStateSetter,
  convId: string,
  controller: AbortController,
  err: unknown,
): boolean {
  if (isHistoryRequestAbort(err, controller.signal)) {
    if (_historyAborts.get(convId) === controller) {
      set({ messagesLoading: false });
    }
    return true;
  }
  const message = errorToMessage(err);
  set((s) =>
    s.currentConvId === convId
      ? { messagesLoading: false, messagesError: message }
      : s,
  );
  return false;
}

export function createConversationActions(
  set: ChatStateSetter,
  get: ChatStateGetter,
): Pick<ChatState, "uploadAttachment" | "loadHistoricalMessages"> {
  return {
    // —— 上传附件：先上后端拿到 image_id，再作为 attachment 挂到 composer ——
    async uploadAttachment(file, opts = {}) {
      const compressed = await compressToMaxDim(file, {
        maxSourceBytes: MAX_UPLOAD_SOURCE_BYTES,
        maxSourceMessage: maxUploadSourceMessage(),
        signal: opts.signal,
      });
      const uploaded = await apiUploadImage(compressed, {
        signal: opts.signal,
      });
      const att: AttachmentImage = {
        id: uploaded.id, // 使用后端返回的 image_id（后续 postMessage 直接用）
        kind: "upload",
        data_url: uploaded.url?.startsWith("data:")
          ? uploaded.url
          : imageBinaryUrl(uploaded.id),
        mime: uploaded.mime ?? compressed.type ?? file.type,
        width: uploaded.width,
        height: uploaded.height,
      };
      return att;
    },

    // —— 载入指定会话的历史文本消息 ——
    // 后端 /conversations/{id}/messages 只返回 MessageOut（不含 generations/images）。
    // 但 store.generations 是全局任务池（切会话不清），所以可以反查 message_id 把
    // 进行中 / 已完成的 Generation 绑回 assistant msg，让切回会话仍能看到进度卡/结果图。
    async loadHistoricalMessages(convId, loadMore = false) {
      const snapshot = get();
      if (shouldSkipHistoryLoad(snapshot, convId, loadMore)) return;

      // 抢占式 abort：上次该会话的首屏历史请求若未完成，直接放弃，避免竞态写入。
      if (!loadMore) {
        abortHistoryRequest(convId);
      }
      const ctl = new AbortController();
      _historyAborts.set(convId, ctl);
      const cursor = loadMore ? snapshot.messagesCursor : null;
      const cached = loadMore ? null : readConversationHistoryCache(convId);
      primeHistoryLoad(set, convId, loadMore, cached);

      try {
        const response = await fetchHistoryPage(
          convId,
          cursor,
          loadMore,
          ctl.signal,
          get,
        );
        const built = buildMessageListState(
          response,
          get().generations,
          get().imagesById,
        );
        rememberMessageListMaterialization(convId, built.materialization);
        const cacheEntry = commitHistoryPage(
          set,
          convId,
          loadMore,
          response,
          built,
        );
        if (cacheEntry) rememberConversationHistoryCache(convId, cacheEntry);
      } catch (err) {
        // AbortError：被新切换覆盖，静默放弃即可。
        if (handleHistoryError(set, convId, ctl, err)) return;
        throw err;
      } finally {
        if (_historyAborts.get(convId) === ctl) _historyAborts.delete(convId);
      }
    },
  };
}
