"use client";

import { create } from "zustand";
import {
  AGENT_MAX_REFERENCES,
  AGENT_NEW_DRAFT_KEY,
  createAgentDraft,
  type AgentAssistantMessage,
  type AgentDraft,
  type AgentDraftAttachment,
  type AgentEventEnvelope,
  type AgentMessage,
  type AgentMessageCreateResult,
  type AgentMessageList,
  type AgentRun,
  type AgentSession,
} from "@/features/agent/model/contracts";
import { applyAgentEvent } from "@/features/agent/model/events";
import {
  adaptAgentMessages,
  mergeAgentMessageLists,
  mergeAgentRun,
  reconcileAgentSnapshot,
} from "@/features/agent/model/reconciliation";
import type { Generation } from "@/lib/types";
import type { PrivateIdentitySnapshot } from "@/lib/auth/privateIdentityEpoch";
import { loadAgentDrafts, saveAgentDrafts } from "./draftPersistence";

export type AgentRealtimeStatus =
  | "idle"
  | "connecting"
  | "open"
  | "closed"
  | "error";

interface AgentStoreState {
  ownerUserId: string | null;
  identityEpoch: number;
  currentSessionId: string | null;
  sessions: Record<string, AgentSession>;
  sessionOrder: string[];
  messagesBySession: Record<string, AgentMessage[]>;
  runsById: Record<string, AgentRun>;
  generationsById: Record<string, Generation>;
  generationSessionIds: Record<string, string>;
  draftsBySession: Record<string, AgentDraft>;
  composerError: string | null;
  realtimeStatus: AgentRealtimeStatus;
  setCurrentSession: (sessionId: string | null) => void;
  replaceSessions: (sessions: AgentSession[]) => void;
  upsertSession: (session: AgentSession) => void;
  removeSession: (sessionId: string) => void;
  applySnapshot: (sessionId: string, snapshot: AgentMessageList) => void;
  applyRunSnapshot: (run: AgentRun) => void;
  appendOptimistic: (input: {
    sessionId: string;
    userMessage: AgentMessage;
    assistantMessage: AgentMessage;
    run: AgentRun;
  }) => void;
  reconcileSubmission: (input: {
    sessionId: string;
    optimisticUserId: string;
    optimisticAssistantId: string;
    result: AgentMessageCreateResult;
  }) => void;
  failOptimistic: (input: {
    sessionId: string;
    runId: string;
    assistantMessageId: string;
    errorCode: string;
    errorMessage: string;
  }) => void;
  discardOptimistic: (input: { sessionId: string; runId: string }) => void;
  applyEvent: (event: AgentEventEnvelope) => boolean;
  setDraft: (sessionId: string | null, patch: Partial<AgentDraft>) => void;
  setDraftText: (sessionId: string | null, text: string) => void;
  addDraftAttachment: (
    sessionId: string | null,
    attachment: AgentDraftAttachment,
  ) => boolean;
  removeDraftAttachment: (sessionId: string | null, imageId: string) => void;
  moveDraftAttachment: (
    sessionId: string | null,
    imageId: string,
    direction: -1 | 1,
  ) => void;
  setDraftAttachmentRole: (
    sessionId: string | null,
    imageId: string,
    role: AgentDraftAttachment["role"],
  ) => void;
  clearDraft: (sessionId: string | null) => void;
  clearDraftContent: (sessionId: string | null) => void;
  migrateDraft: (fromSessionId: string | null, toSessionId: string) => void;
  setComposerError: (message: string | null) => void;
  setRealtimeStatus: (status: AgentRealtimeStatus) => void;
  resetForIdentity: (identity: PrivateIdentitySnapshot) => void;
}

function draftKey(sessionId: string | null): string {
  return sessionId ?? AGENT_NEW_DRAFT_KEY;
}

function currentDraft(
  drafts: Record<string, AgentDraft>,
  sessionId: string | null,
): AgentDraft {
  return drafts[draftKey(sessionId)] ?? createAgentDraft();
}

function persist(state: AgentStoreState, drafts: Record<string, AgentDraft>): void {
  saveAgentDrafts(state.ownerUserId, drafts);
}

function initialState(identity: PrivateIdentitySnapshot): Pick<
  AgentStoreState,
  | "ownerUserId"
  | "identityEpoch"
  | "currentSessionId"
  | "sessions"
  | "sessionOrder"
  | "messagesBySession"
  | "runsById"
  | "generationsById"
  | "generationSessionIds"
  | "draftsBySession"
  | "composerError"
  | "realtimeStatus"
> {
  return {
    ownerUserId: identity.userId,
    identityEpoch: identity.epoch,
    currentSessionId: null,
    sessions: {},
    sessionOrder: [],
    messagesBySession: {},
    runsById: {},
    generationsById: {},
    generationSessionIds: {},
    draftsBySession: identity.userId ? loadAgentDrafts(identity.userId) : {},
    composerError: null,
    realtimeStatus: "idle",
  };
}

function createAgentStore() {
  return create<AgentStoreState>((set, get) => ({
    ...initialState({ userId: null, epoch: 0 }),
    setCurrentSession: (currentSessionId) =>
      set({ currentSessionId, composerError: null }),
    replaceSessions: (sessions) =>
      set((state) => {
        const next = { ...state.sessions };
        for (const session of sessions) next[session.id] = session;
        return {
          sessions: next,
          sessionOrder: sessions.map((session) => session.id),
        };
      }),
    upsertSession: (session) =>
      set((state) => ({
        sessions: { ...state.sessions, [session.id]: session },
        sessionOrder: state.sessionOrder.includes(session.id)
          ? state.sessionOrder
          : [session.id, ...state.sessionOrder],
      })),
    removeSession: (sessionId) =>
      set((state) => {
        const sessions = { ...state.sessions };
        const messagesBySession = { ...state.messagesBySession };
        const draftsBySession = { ...state.draftsBySession };
        delete sessions[sessionId];
        delete messagesBySession[sessionId];
        delete draftsBySession[sessionId];
        persist(state, draftsBySession);
        return {
          sessions,
          messagesBySession,
          draftsBySession,
          sessionOrder: state.sessionOrder.filter((id) => id !== sessionId),
          currentSessionId:
            state.currentSessionId === sessionId ? null : state.currentSessionId,
        };
      }),
    applySnapshot: (sessionId, snapshot) =>
      set((state) => {
        const reconciled = reconcileAgentSnapshot(
          state.messagesBySession[sessionId] ?? [],
          state.runsById,
          snapshot,
          sessionId,
        );
        const generationsById = { ...state.generationsById };
        const generationSessionIds = { ...state.generationSessionIds };
        for (const [generationId, generation] of Object.entries(
          reconciled.generations.byId,
        )) {
          generationsById[generationId] = generation;
          generationSessionIds[generationId] = sessionId;
        }
        return {
          messagesBySession: {
            ...state.messagesBySession,
            [sessionId]: reconciled.messages,
          },
          runsById: reconciled.runs,
          generationsById,
          generationSessionIds,
        };
      }),
    applyRunSnapshot: (run) =>
      set((state) => ({
        runsById: {
          ...state.runsById,
          [run.id]: mergeAgentRun(state.runsById[run.id], run),
        },
      })),
    appendOptimistic: ({ sessionId, userMessage, assistantMessage, run }) =>
      set((state) => ({
        messagesBySession: {
          ...state.messagesBySession,
          [sessionId]: mergeAgentMessageLists(
            state.messagesBySession[sessionId] ?? [],
            [userMessage, assistantMessage],
          ),
        },
        runsById: { ...state.runsById, [run.id]: run },
      })),
    reconcileSubmission: ({
      sessionId,
      optimisticUserId,
      optimisticAssistantId,
      result,
    }) =>
      set((state) => {
        const remaining = (state.messagesBySession[sessionId] ?? []).filter(
          (message) =>
            message.id !== optimisticUserId &&
            message.id !== optimisticAssistantId,
        );
        const incoming = adaptAgentMessages(
          [result.user_message, result.assistant_message],
          [result.agent_run],
        );
        const runsById = { ...state.runsById };
        delete runsById[`optimistic:${optimisticAssistantId}`];
        runsById[result.agent_run.id] = mergeAgentRun(
          runsById[result.agent_run.id],
          result.agent_run,
        );
        return {
          messagesBySession: {
            ...state.messagesBySession,
            [sessionId]: mergeAgentMessageLists(remaining, incoming),
          },
          runsById,
        };
      }),
    failOptimistic: ({
      sessionId,
      runId,
      assistantMessageId,
      errorCode,
      errorMessage,
    }) =>
      set((state) => {
        const run = state.runsById[runId];
        const messages = state.messagesBySession[sessionId] ?? [];
        return {
          runsById: run
            ? {
                ...state.runsById,
                [runId]: {
                  ...run,
                  status: "failed",
                  error_code: errorCode,
                  error_message: errorMessage,
                  finished_at: new Date().toISOString(),
                  updated_at: new Date().toISOString(),
                },
              }
            : state.runsById,
          messagesBySession: {
            ...state.messagesBySession,
            [sessionId]: messages.map((message) =>
              message.id === assistantMessageId && message.role === "assistant"
                ? { ...message, status: "failed" as const }
                : message,
            ),
          },
        };
      }),
    discardOptimistic: ({ sessionId, runId }) =>
      set((state) => {
        const run = state.runsById[runId];
        if (!run) return state;
        const runsById = { ...state.runsById };
        delete runsById[runId];
        return {
          runsById,
          messagesBySession: {
            ...state.messagesBySession,
            [sessionId]: (state.messagesBySession[sessionId] ?? []).filter(
              (message) =>
                message.id !== run.user_message_id &&
                message.id !== run.assistant_message_id,
            ),
          },
        };
      }),
    applyEvent: (event) => {
      let accepted = false;
      set((state) => {
        const run = state.runsById[event.agent_run_id];
        const messages = state.messagesBySession[event.agent_session_id] ?? [];
        const index = messages.findIndex(
          (message) => message.id === event.assistant_message_id,
        );
        const message = messages[index];
        if (!run || !message || message.role !== "assistant") return state;
        const decision = applyAgentEvent(
          run,
          message as AgentAssistantMessage,
          event,
        );
        if (!decision.accepted) return state;
        accepted = true;
        const nextMessages = [...messages];
        nextMessages[index] = decision.nextMessage;
        return {
          runsById: {
            ...state.runsById,
            [run.id]: decision.nextRun,
          },
          messagesBySession: {
            ...state.messagesBySession,
            [event.agent_session_id]: nextMessages,
          },
        };
      });
      return accepted;
    },
    setDraft: (sessionId, patch) =>
      set((state) => {
        const key = draftKey(sessionId);
        const previous = currentDraft(state.draftsBySession, sessionId);
        const nextDraft = {
          ...previous,
          ...patch,
          imageDefaults: {
            ...previous.imageDefaults,
            ...patch.imageDefaults,
          },
        };
        const draftsBySession = {
          ...state.draftsBySession,
          [key]: nextDraft,
        };
        persist(state, draftsBySession);
        return { draftsBySession };
      }),
    setDraftText: (sessionId, text) => get().setDraft(sessionId, { text }),
    addDraftAttachment: (sessionId, attachment) => {
      const draft = currentDraft(get().draftsBySession, sessionId);
      if (
        draft.attachments.length >= AGENT_MAX_REFERENCES ||
        draft.attachments.some((item) => item.imageId === attachment.imageId)
      ) {
        return false;
      }
      get().setDraft(sessionId, {
        attachments: [...draft.attachments, attachment],
      });
      return true;
    },
    removeDraftAttachment: (sessionId, imageId) => {
      const draft = currentDraft(get().draftsBySession, sessionId);
      get().setDraft(sessionId, {
        attachments: draft.attachments.filter((item) => item.imageId !== imageId),
      });
    },
    moveDraftAttachment: (sessionId, imageId, direction) => {
      const draft = currentDraft(get().draftsBySession, sessionId);
      const index = draft.attachments.findIndex((item) => item.imageId === imageId);
      const target = index + direction;
      if (index < 0 || target < 0 || target >= draft.attachments.length) return;
      const attachments = [...draft.attachments];
      [attachments[index], attachments[target]] = [
        attachments[target],
        attachments[index],
      ];
      get().setDraft(sessionId, { attachments });
    },
    setDraftAttachmentRole: (sessionId, imageId, role) => {
      const draft = currentDraft(get().draftsBySession, sessionId);
      get().setDraft(sessionId, {
        attachments: draft.attachments.map((item) =>
          item.imageId === imageId ? { ...item, role } : item,
        ),
      });
    },
    clearDraft: (sessionId) =>
      set((state) => {
        const draftsBySession = { ...state.draftsBySession };
        delete draftsBySession[draftKey(sessionId)];
        persist(state, draftsBySession);
        return { draftsBySession, composerError: null };
      }),
    clearDraftContent: (sessionId) => {
      get().setDraft(sessionId, { text: "", attachments: [] });
      set({ composerError: null });
    },
    migrateDraft: (fromSessionId, toSessionId) =>
      set((state) => {
        const sourceKey = draftKey(fromSessionId);
        const draft = state.draftsBySession[sourceKey];
        if (!draft || sourceKey === toSessionId) return state;
        const draftsBySession = { ...state.draftsBySession };
        delete draftsBySession[sourceKey];
        draftsBySession[toSessionId] = draft;
        persist(state, draftsBySession);
        return { draftsBySession };
      }),
    setComposerError: (composerError) => set({ composerError }),
    setRealtimeStatus: (realtimeStatus) => set({ realtimeStatus }),
    resetForIdentity: (identity) => set(initialState(identity)),
  }));
}

type AgentStoreHook = ReturnType<typeof createAgentStore>;
let browserAgentStore: AgentStoreHook | null = null;

function agentStore(): AgentStoreHook {
  if (typeof window === "undefined") return createAgentStore();
  browserAgentStore ??= createAgentStore();
  return browserAgentStore;
}

export const useAgentStore = new Proxy(
  ((...args: Parameters<AgentStoreHook>) => agentStore()(...args)) as AgentStoreHook,
  {
    get: (_target, property, receiver) =>
      Reflect.get(agentStore(), property, receiver),
    set: (_target, property, value, receiver) =>
      Reflect.set(agentStore(), property, value, receiver),
    has: (_target, property) => property in agentStore(),
    ownKeys: () => Reflect.ownKeys(agentStore()),
    getOwnPropertyDescriptor: (_target, property) =>
      Reflect.getOwnPropertyDescriptor(agentStore(), property),
  },
) as AgentStoreHook;

export function selectAgentDraft(
  state: AgentStoreState,
  sessionId: string | null,
): AgentDraft {
  return currentDraft(state.draftsBySession, sessionId);
}
