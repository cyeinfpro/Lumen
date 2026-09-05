"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  AGENT_EVENT_NAMES,
  AGENT_NEW_DRAFT_KEY,
  createAgentDraft,
  isAgentRunTerminal,
  type AgentAssistantMessage,
  type AgentDraftAttachment,
  type AgentDraftFile,
  type AgentImageDefaults,
  type AgentSession,
  type AgentSessionPatchInput,
  type AgentStatus,
} from "../model/contracts";
import { getAgentActiveRun, listAgentMessages } from "../api/agentApi";
import {
  useAgentActiveRunQuery,
  useAgentMessagesQuery,
  useBranchAgentSessionMutation,
  useCancelAgentRunMutation,
  useContinueAgentRunMutation,
  useCreateAgentSessionMutation,
  useDeleteAgentSessionMutation,
  useEjectAgentSessionImageMutation,
  usePatchAgentSessionMutation,
  useAgentSessionImagesQuery,
} from "../api/queries";
import { parseAgentEventEnvelope } from "../model/events";
import { agentErrorPresentation } from "../model/errors";
import { flattenFeed, useStreamFeedQuery } from "@/features/assets";
import { useSSE, type SSEHandlers } from "@/features/realtime";
import { useSystemPromptsQuery } from "@/lib/queries";
import { qk } from "@/lib/queries/queryKeys";
import { useUserQueryScope } from "@/lib/queries/userScope";
import { getPrivateIdentitySnapshot } from "@/lib/auth/privateIdentityEpoch";
import {
  selectAgentDraft,
  useAgentStore,
  type AgentRealtimeStatus,
} from "@/store/agent/useAgentStore";
import type { AttachmentRole } from "@/lib/types";
import {
  useAgentScrollTargetPagination,
  useAgentSessionDirectory,
} from "./useAgentSessionDirectory";
import {
  AGENT_GENERATION_EVENT_NAMES,
  AgentRefreshCoordinator,
  selectAgentGenerationChannelIds,
} from "./agentRealtime";
import { mergeAgentImageDefaults } from "./agentImageDefaults";
import { useAgentSnapshotPolling } from "./useAgentSnapshotPolling";
import { useAgentMediaActions } from "./useAgentMediaActions";
import {
  createSessionForSubmission,
  agentMessageBody,
  acquireAgentSubmissionFence,
  postAgentMessageWithTransportRetry,
  releaseAgentSubmissionFence,
  reconcileFailedAgentSubmission,
  stageOptimisticSubmission,
  uniqueAgentId,
} from "./agentSubmission";
import {
  AgentWorkspaceView,
  agentChannels,
  busySessionId,
  removingImageId,
  snapshotPollInterval,
} from "./agentWorkspaceView";

export function AgentWorkspaceController({
  platform,
  agentStatus,
}: {
  platform: "desktop" | "mobile";
  agentStatus: AgentStatus;
}) {
  const toolGatewayConfigured = agentStatus.tool_gateway_configured;
  const userScope = useUserQueryScope();
  const queryClient = useQueryClient();
  const search = useSearchParams();
  const [submitting, setSubmitting] = useState(false);
  const [composerAction, setComposerAction] = useState<{ href: string; label: string } | null>(null);
  const submissionRef = useRef(false);
  const refreshCoordinator = useMemo(() => new AgentRefreshCoordinator(), []);
  const sseTransportStatusRef = useRef<AgentRealtimeStatus>("idle");

  const currentSessionId = useAgentStore((state) => state.currentSessionId);
  const messagesBySession = useAgentStore((state) => state.messagesBySession);
  const runsById = useAgentStore((state) => state.runsById);
  const generationsById = useAgentStore((state) => state.generationsById);
  const generationSessionIds = useAgentStore((state) => state.generationSessionIds);
  const draftsBySession = useAgentStore((state) => state.draftsBySession);
  const composerError = useAgentStore((state) => state.composerError);
  const realtimeStatus = useAgentStore((state) => state.realtimeStatus);
  const removeSession = useAgentStore((state) => state.removeSession);
  const applySnapshot = useAgentStore((state) => state.applySnapshot);
  const applyRunSnapshot = useAgentStore((state) => state.applyRunSnapshot);
  const appendOptimistic = useAgentStore((state) => state.appendOptimistic);
  const reconcileSubmission = useAgentStore((state) => state.reconcileSubmission);
  const failOptimistic = useAgentStore((state) => state.failOptimistic);
  const discardOptimistic = useAgentStore((state) => state.discardOptimistic);
  const applyEvent = useAgentStore((state) => state.applyEvent);
  const setDraft = useAgentStore((state) => state.setDraft);
  const setDraftText = useAgentStore((state) => state.setDraftText);
  const addDraftAttachment = useAgentStore((state) => state.addDraftAttachment);
  const removeDraftAttachment = useAgentStore((state) => state.removeDraftAttachment);
  const moveDraftAttachment = useAgentStore((state) => state.moveDraftAttachment);
  const setDraftAttachmentRole = useAgentStore((state) => state.setDraftAttachmentRole);
  const addDraftFile = useAgentStore((state) => state.addDraftFile);
  const removeDraftFile = useAgentStore((state) => state.removeDraftFile);
  const clearDraftContent = useAgentStore((state) => state.clearDraftContent);
  const migrateDraft = useAgentStore((state) => state.migrateDraft);
  const setComposerError = useAgentStore((state) => state.setComposerError);
  const setRealtimeStatus = useAgentStore((state) => state.setRealtimeStatus);
  const requestedSessionId = search.get("session");
  const {
    currentSession,
    sessionsQuery,
    sidebarSessions,
    sessionSearch,
    setSessionSearch,
    selectWithRoute,
    setCurrentSession,
    upsertSession,
  } = useAgentSessionDirectory({
    currentSessionId,
    requestedSessionId,
    applyRunSnapshot,
  });
  const messages = useMemo(
    () => (currentSessionId ? messagesBySession[currentSessionId] ?? [] : []),
    [currentSessionId, messagesBySession],
  );
  const draft = useMemo(
    () =>
      draftsBySession[currentSessionId ?? AGENT_NEW_DRAFT_KEY] ??
      createAgentDraft(),
    [currentSessionId, draftsBySession],
  );

  useEffect(() => {
    if (
      !draft.model ||
      agentStatus.models.some((option) => option.model === draft.model)
    ) {
      return;
    }
    setDraft(currentSessionId, { model: null });
  }, [agentStatus.models, currentSessionId, draft.model, setDraft]);

  useEffect(() => {
    if (!currentSession || draftsBySession[currentSession.id]) return;
    setDraft(currentSession.id, {
      allowImage: currentSession.allow_image,
      allowWebSearch: currentSession.allow_web_search,
      allowFileTools: currentSession.allow_file_tools,
      imageDefaults: currentSession.image_defaults,
    });
  }, [currentSession, draftsBySession, setDraft]);

  const messagesQuery = useAgentMessagesQuery(currentSessionId);
  const activeRunQuery = useAgentActiveRunQuery(currentSessionId);
  const sessionImagesQuery = useAgentSessionImagesQuery(currentSessionId);
  useEffect(() => {
    if (currentSessionId && messagesQuery.data) {
      for (const page of messagesQuery.data.pages) {
        applySnapshot(currentSessionId, page);
      }
    }
  }, [applySnapshot, currentSessionId, messagesQuery.data]);
  useEffect(() => {
    if (activeRunQuery.data) applyRunSnapshot(activeRunQuery.data);
  }, [activeRunQuery.data, applyRunSnapshot]);
  const scrollToMessageId = search.get("scrollTo");
  useAgentScrollTargetPagination({
    currentSessionId,
    scrollToMessageId,
    messagesQuery,
  });

  const activeRun = useMemo(() => {
    const candidates = Object.values(runsById)
      .filter(
        (run) =>
          run.agent_session_id === currentSessionId &&
          !isAgentRunTerminal(run.status),
      )
      .sort((left, right) => right.created_at.localeCompare(left.created_at));
    return candidates[0] ?? null;
  }, [currentSessionId, runsById]);
  const snapshotPollIntervalMs = snapshotPollInterval(Boolean(activeRun));
  const refreshSnapshot = useCallback(
    async (signal?: AbortSignal) => {
      const sessionId = useAgentStore.getState().currentSessionId;
      if (!sessionId) return;
      const identity = getPrivateIdentitySnapshot();
      const [snapshot, run] = await Promise.all([
        listAgentMessages(sessionId, { limit: 100, includeTasks: true, signal }),
        getAgentActiveRun(sessionId, signal),
      ]);
      const currentIdentity = getPrivateIdentitySnapshot();
      if (
        useAgentStore.getState().currentSessionId !== sessionId ||
        currentIdentity.userId !== identity.userId ||
        currentIdentity.epoch !== identity.epoch
      ) return;
      applySnapshot(sessionId, snapshot);
      if (run) applyRunSnapshot(run);
    },
    [applyRunSnapshot, applySnapshot],
  );
  const coordinatedRefresh = useCallback((signal?: AbortSignal) =>
    refreshCoordinator.request(() => refreshSnapshot(signal)),
  [refreshCoordinator, refreshSnapshot]);
  const requestRefresh = useCallback(() => {
    void coordinatedRefresh().catch(() => undefined);
  }, [coordinatedRefresh]);

  const pollingInput = useMemo(
    () => ({
      sessionId: currentSessionId,
      intervalMs: snapshotPollIntervalMs,
      refresh: (signal: AbortSignal) => coordinatedRefresh(signal),
      setStatus: (status: AgentRealtimeStatus) => {
        if (sseTransportStatusRef.current !== "open") setRealtimeStatus(status);
      },
    }),
    [currentSessionId, coordinatedRefresh, setRealtimeStatus, snapshotPollIntervalMs],
  );
  useAgentSnapshotPolling(pollingInput);

  const currentGenerationIds = useMemo(
    () =>
      selectAgentGenerationChannelIds(
        generationsById,
        generationSessionIds,
        currentSessionId,
      ),
    [currentSessionId, generationSessionIds, generationsById],
  );
  const channels = useMemo(
    () => agentChannels(currentSessionId, currentGenerationIds),
    [currentGenerationIds, currentSessionId],
  );
  const identityEpoch = getPrivateIdentitySnapshot().epoch;
  const sseScope = `${userScope.userId ?? "unknown"}:${identityEpoch}:${channels.join(",")}`;
  const handlers = useMemo<SSEHandlers>(() => {
    const entries: Array<[string, (payload: unknown) => void]> = [];
    for (const name of AGENT_EVENT_NAMES) {
      entries.push([
        name,
        (payload) => {
          const event = parseAgentEventEnvelope(name, payload);
          const accepted = applyEvent(event);
          if (
            accepted &&
            (name.startsWith("agent.tool.") ||
              name.startsWith("agent.run.") ||
              event.snapshot_required === true)
          ) {
            requestRefresh();
          }
        },
      ]);
    }
    for (const name of AGENT_GENERATION_EVENT_NAMES) {
      entries.push([name, requestRefresh]);
    }
    return Object.fromEntries(entries);
  }, [applyEvent, requestRefresh]);
  const { status: sseStatus, reconnect } = useSSE(channels, handlers, {
    scopeIdentity: sseScope,
    isScopeCurrent: (scope) => {
      const identity = getPrivateIdentitySnapshot();
      return (
        scope === sseScope &&
        identity.userId === userScope.userId &&
        identity.epoch === identityEpoch &&
        useAgentStore.getState().currentSessionId === currentSessionId
      );
    },
    recoverSnapshot: async (_scopes, _reason, signal, context) => {
      if (!context.isCurrent()) throw new DOMException("stale Agent scope", "AbortError");
      await coordinatedRefresh(signal);
      if (!context.isCurrent()) throw new DOMException("stale Agent scope", "AbortError");
      return { syncedAt: Date.now() };
    },
  });
  useEffect(() => {
    const status = channels.length ? sseStatus : "idle";
    sseTransportStatusRef.current = status;
    setRealtimeStatus(status);
  }, [channels.length, setRealtimeStatus, sseStatus]);

  const createMutation = useCreateAgentSessionMutation();
  const branchMutation = useBranchAgentSessionMutation();
  const patchMutation = usePatchAgentSessionMutation();
  const deleteMutation = useDeleteAgentSessionMutation();
  const cancelMutation = useCancelAgentRunMutation();
  const continueMutation = useContinueAgentRunMutation();
  const ejectImageMutation = useEjectAgentSessionImageMutation();
  const promptsQuery = useSystemPromptsQuery({ enabled: userScope.enabled });
  const assetQuery = useStreamFeedQuery({}, 24);
  const assetItems = useMemo(() => flattenFeed(assetQuery.data), [assetQuery.data]);

  const selectSession = useCallback((sessionId: string) => selectWithRoute(sessionId), [selectWithRoute]);
  const branchSession = useCallback(async () => {
    const source = useAgentStore.getState().currentSessionId;
    if (!source || branchMutation.isPending || activeRun) return;
    try {
      const branched = await branchMutation.mutateAsync({ sessionId: source });
      upsertSession(branched);
      selectWithRoute(branched.id);
    } catch (error) {
      setComposerError(agentErrorPresentation(error).detail);
    }
  }, [
    activeRun,
    branchMutation,
    selectWithRoute,
    setComposerError,
    upsertSession,
  ]);
  const createSession = useCallback(async () => {
    try {
      const session = await createMutation.mutateAsync({
        image_defaults: draft.imageDefaults,
        allow_image: draft.allowImage && toolGatewayConfigured,
        allow_web_search: draft.allowWebSearch,
        allow_file_tools: draft.allowFileTools,
      });
      upsertSession(session);
      migrateDraft(null, session.id);
      selectWithRoute(session.id);
    } catch (error) {
      const presentation = agentErrorPresentation(error);
      setComposerError(presentation.detail);
      setComposerAction(
        presentation.href && presentation.actionLabel
          ? { href: presentation.href, label: presentation.actionLabel }
          : null,
      );
    }
  }, [
    createMutation,
    draft.allowFileTools,
    draft.allowImage,
    draft.allowWebSearch,
    draft.imageDefaults,
    migrateDraft,
    selectWithRoute,
    setComposerError,
    toolGatewayConfigured,
    upsertSession,
  ]);

  const patchSession = useCallback((patch: AgentSessionPatchInput) => {
    const sessionId = useAgentStore.getState().currentSessionId;
    if (!sessionId) return;
    patchMutation.mutate({ sessionId, patch }, { onSuccess: upsertSession });
  }, [patchMutation, upsertSession]);

  const submit = useCallback(async () => {
    if (submitting || createMutation.isPending || activeRun) {
      return;
    }
    setComposerError(null);
    setComposerAction(null);
    let sessionId = useAgentStore.getState().currentSessionId;
    let sendDraft = selectAgentDraft(useAgentStore.getState(), sessionId);
    let optimistic: ReturnType<typeof stageOptimisticSubmission> | null = null;
    if (
      !sendDraft.text.trim() &&
      sendDraft.attachments.length === 0 &&
      sendDraft.files.length === 0
    ) return;
    if (!acquireAgentSubmissionFence(submissionRef)) return;
    setSubmitting(true);
    try {
      if (!sessionId) {
        sessionId = await createSessionForSubmission({
          draft: sendDraft,
          toolGatewayConfigured,
          create: createMutation.mutateAsync,
          upsert: upsertSession,
          migrateDraft,
          select: setCurrentSession,
          navigate: selectWithRoute,
        });
        sendDraft = selectAgentDraft(useAgentStore.getState(), sessionId);
      }
      const idempotencyKey = uniqueAgentId("agent-message").slice(0, 96);
      optimistic = stageOptimisticSubmission({
        sessionId,
        draft: sendDraft,
        append: appendOptimistic,
        idempotencyKey,
      });
      const body = agentMessageBody(
        sendDraft,
        toolGatewayConfigured,
        idempotencyKey,
      );
      const result = await postAgentMessageWithTransportRetry(sessionId, body);
      reconcileSubmission({
        sessionId,
        optimisticUserId: optimistic.userMessageId,
        optimisticAssistantId: optimistic.assistantMessageId,
        result,
      });
      clearDraftContent(sessionId);
      const existingSession = useAgentStore.getState().sessions[sessionId];
      if (existingSession) {
        upsertSession({
          ...existingSession,
          image_defaults: sendDraft.imageDefaults,
          allow_image: sendDraft.allowImage && toolGatewayConfigured,
          allow_web_search: sendDraft.allowWebSearch,
          allow_file_tools: sendDraft.allowFileTools,
        });
      }
      void queryClient.invalidateQueries({ queryKey: qk.user(userScope.userId).agentAll() });
      requestRefresh();
    } catch (error) {
      reconcileFailedAgentSubmission({
        sessionId,
        optimistic,
        error,
        discard: discardOptimistic,
        fail: failOptimistic,
      });
      const presentation = agentErrorPresentation(error);
      setComposerError(presentation.detail);
      setComposerAction(
        presentation.href && presentation.actionLabel
          ? { href: presentation.href, label: presentation.actionLabel }
          : null,
      );
      requestRefresh();
    } finally {
      releaseAgentSubmissionFence(submissionRef);
      setSubmitting(false);
    }
  }, [activeRun, appendOptimistic, clearDraftContent, createMutation, discardOptimistic, failOptimistic, migrateDraft, queryClient, reconcileSubmission, requestRefresh, selectWithRoute, setComposerError, setCurrentSession, submitting, toolGatewayConfigured, upsertSession, userScope.userId]);

  const deleteSession = useCallback(async (sessionId: string) => {
    await deleteMutation.mutateAsync(sessionId);
    removeSession(sessionId);
    const next = useAgentStore.getState().sessionOrder.find((id) => id !== sessionId);
    if (next) selectWithRoute(next);
  }, [deleteMutation, removeSession, selectWithRoute]);

  const updateDefaults = useCallback((patch: Partial<AgentImageDefaults>) => {
    const state = useAgentStore.getState();
    const current = selectAgentDraft(state, state.currentSessionId);
    setDraft(state.currentSessionId, {
      imageDefaults: mergeAgentImageDefaults(current.imageDefaults, patch),
    });
  }, [setDraft]);

  const {
    upload,
    previewAttachment,
    previewGeneration,
    addGenerationReference,
    pickAsset,
  } = useAgentMediaActions(addDraftAttachment, setComposerError);

  const continueFrom = useCallback((assistant: AgentAssistantMessage) => {
    if (!assistant.agentRunId) return;
    const source = useAgentStore.getState().runsById[assistant.agentRunId];
    if (!source?.continuable || continueMutation.isPending) return;
    continueMutation.mutate(
      {
        runId: source.id,
        idempotencyKey: uniqueAgentId("agent-continue").slice(0, 96),
      },
      {
        onSuccess: (run) => {
          applyRunSnapshot(run);
          requestRefresh();
        },
        onError: (error) => {
          setComposerError(agentErrorPresentation(error).detail);
        },
      },
    );
  }, [applyRunSnapshot, continueMutation, requestRefresh, setComposerError]);

  const workspaceProps = {
    sessions: sidebarSessions,
    currentSession,
    messages,
    runsById,
    generationsById,
    draft,
    sessionsLoading: sessionsQuery.isLoading,
    messagesLoading: messagesQuery.isLoading,
    sessionsHaveMore: Boolean(sessionsQuery.hasNextPage),
    sessionsLoadingMore: sessionsQuery.isFetchingNextPage,
    sessionSearch,
    messagesHaveMore: Boolean(messagesQuery.hasNextPage),
    messagesLoadingMore: messagesQuery.isFetchingNextPage,
    messagesError: messagesQuery.error?.message ?? null,
    creating: createMutation.isPending,
    branching: branchMutation.isPending,
    submitting,
    stopping: cancelMutation.isPending,
    busySessionId: busySessionId({
      patching: patchMutation.isPending,
      patchSessionId: patchMutation.variables?.sessionId,
      deleting: deleteMutation.isPending,
      deleteSessionId: deleteMutation.variables,
    }),
    activeRun,
    realtimeStatus,
    toolGatewayConfigured,
    defaultModel: agentStatus.default_model,
    modelOptions: agentStatus.models,
    prompts: (promptsQuery.data?.items ?? []).map((prompt) => ({ id: prompt.id, name: prompt.name })),
    sessionSaving: patchMutation.isPending,
    sessionImages: sessionImagesQuery.data ?? null,
    sessionImagesLoading: sessionImagesQuery.isLoading,
    sessionImageRemovingId: removingImageId(
      ejectImageMutation.isPending,
      ejectImageMutation.variables?.imageId,
    ),
    scrollToMessageId,
    assetItems,
    assetsLoading: assetQuery.isLoading || assetQuery.isFetchingNextPage,
    assetsHaveMore: Boolean(assetQuery.hasNextPage),
    onLoadMoreAssets: () => void assetQuery.fetchNextPage(),
    onLoadMoreSessions: () => void sessionsQuery.fetchNextPage(),
    onSessionSearchChange: setSessionSearch,
    onLoadOlderMessages: async () => {
      await messagesQuery.fetchNextPage();
    },
    onCreateSession: () => void createSession(),
    onBranchSession: () => void branchSession(),
    onSelectSession: selectSession,
    onRenameSession: (sessionId: string, title: string) =>
      patchMutation.mutate({ sessionId, patch: { title } }, { onSuccess: upsertSession }),
    onArchiveSession: (session: AgentSession) =>
      patchMutation.mutate(
        { sessionId: session.id, patch: { archived: !session.archived } },
        { onSuccess: upsertSession },
      ),
    onDeleteSession: deleteSession,
    onPatchSession: patchSession,
    onEjectSessionImage: (imageId: string) => {
      const sessionId = useAgentStore.getState().currentSessionId;
      if (!sessionId) return;
      ejectImageMutation.mutate(
        { sessionId, imageId },
        {
          onError: (error) => {
            setComposerError(agentErrorPresentation(error).detail);
          },
        },
      );
    },
    onRetryMessages: () => {
      void messagesQuery.refetch();
      reconnect();
    },
    onTextChange: (text: string) => setDraftText(currentSessionId, text),
    onDraftChange: (patch: Parameters<typeof setDraft>[1]) => setDraft(currentSessionId, patch),
    onDefaultsChange: updateDefaults,
    onUpload: upload,
    onAddAttachment: (attachment: AgentDraftAttachment) => addDraftAttachment(currentSessionId, attachment),
    onRemoveAttachment: (imageId: string) => removeDraftAttachment(currentSessionId, imageId),
    onMoveAttachment: (imageId: string, direction: -1 | 1) => moveDraftAttachment(currentSessionId, imageId, direction),
    onRoleChange: (imageId: string, role: AttachmentRole) => setDraftAttachmentRole(currentSessionId, imageId, role),
    onAddFile: (file: AgentDraftFile) => addDraftFile(currentSessionId, file),
    onRemoveFile: (name: string) => removeDraftFile(currentSessionId, name),
    onPreviewAttachment: previewAttachment,
    onPickAsset: pickAsset,
    onPreviewGeneration: previewGeneration,
    onUseReference: addGenerationReference,
    onContinue: continueFrom,
    onSubmit: () => void submit(),
    onStop: () => activeRun && cancelMutation.mutate(activeRun.id, { onSuccess: applyRunSnapshot }),
    composerError,
    composerAction,
    onComposerError: (message: string | null) => {
      setComposerError(message);
      setComposerAction(null);
    },
  };

  return <AgentWorkspaceView platform={platform} props={workspaceProps} />;
}
