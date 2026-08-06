import {
  ApiError,
  getTask as apiGetTask,
  listMyActiveTasks as apiListMyActiveTasks,
  type BackendCompletion,
  type BackendGeneration,
} from "../../lib/apiClient";
import { logWarn } from "../../lib/logger";
import type { AssistantMessage, Generation } from "../../lib/types";
import {
  coerceGenerationStage,
  coerceGenerationStatus,
  generationExplainabilityFromBackend,
  generationTaskMetaFromBackend,
  isInflightGeneration,
  mergeUnknownActiveGenerations,
  type GenerationExplainabilityMeta,
} from "@/features/generation";
import {
  applyCompletionSnapshot,
  isTerminalTaskStatus,
} from "./messageReconciliation";
import { isoToMs } from "./payload";
import type { RequestFence } from "./requestGuards";
import type {
  ChatState,
  ChatStateGetter,
  ChatStateSetter,
  PollInflightOptions,
  TaskRecoveryOutcome,
} from "./types";

export type TaskRecoveryActions = Pick<
  ChatState,
  "refreshCompletionText" | "pollInflightTasks" | "hydrateActiveTasks"
>;

type TaskRecoveryDependencies = {
  flushCompletionStreamPatches: () => void;
  userSessionFence: RequestFence;
  isAbortRequest: (error: unknown, signal: AbortSignal) => boolean;
  errorToMessage: (error: unknown) => string;
  getGenerationTask?: (
    generationId: string,
    opts?: { signal?: AbortSignal },
  ) => Promise<BackendGeneration>;
  getCompletionTask?: (
    completionId: string,
    opts?: { signal?: AbortSignal },
  ) => Promise<BackendCompletion>;
  listActiveTasks?: typeof apiListMyActiveTasks;
};

type TaskRecoveryRuntime = Required<
  Pick<
    TaskRecoveryDependencies,
    "getGenerationTask" | "getCompletionTask" | "listActiveTasks"
  >
> &
  Omit<
    TaskRecoveryDependencies,
    "getGenerationTask" | "getCompletionTask" | "listActiveTasks"
  >;

type ActiveTaskHydrateRequest = {
  promise: Promise<TaskRecoveryOutcome>;
  signal?: AbortSignal;
};

type TaskPollResult =
  | { status: "complete"; refetchConvId: string | null }
  | { status: "aborted"; refetchConvId: null }
  | { status: "failed"; refetchConvId: null; error: unknown };

type HydrateSnapshotContext = {
  requestedUserId: string;
  userFence: number;
  signal: AbortSignal | undefined;
  get: ChatStateGetter;
  set: ChatStateSetter;
  dependencies: TaskRecoveryRuntime;
};

const COMPLETE_RECOVERY: TaskRecoveryOutcome = { status: "complete" };
const ABORTED_RECOVERY: TaskRecoveryOutcome = { status: "aborted" };

function withDefaultApis(
  dependencies: TaskRecoveryDependencies,
): TaskRecoveryRuntime {
  return {
    ...dependencies,
    getGenerationTask:
      dependencies.getGenerationTask ??
      ((generationId, opts) =>
        apiGetTask("generations", generationId, opts)),
    getCompletionTask:
      dependencies.getCompletionTask ??
      ((completionId, opts) =>
        apiGetTask("completions", completionId, opts)),
    listActiveTasks: dependencies.listActiveTasks ?? apiListMyActiveTasks,
  };
}

function isInflightAssistant(message: AssistantMessage): boolean {
  return message.status === "pending" || message.status === "streaming";
}

function selectableGenerationIds(
  state: ChatState,
  allowedIds: Set<string> | null,
): string[] {
  return Object.values(state.generations)
    .filter(
      (generation) =>
        isInflightGeneration(generation) &&
        !generation.id.startsWith("opt-") &&
        (!allowedIds || allowedIds.has(generation.id)),
    )
    .map((generation) => generation.id);
}

function selectableCompletionIds(
  state: ChatState,
  allowedIds: Set<string> | null,
): string[] {
  const ids: string[] = [];
  for (const message of state.messages) {
    if (message.role !== "assistant" || !isInflightAssistant(message)) continue;
    const completionId = message.completion_id;
    if (
      completionId &&
      !completionId.startsWith("opt-") &&
      (!allowedIds || allowedIds.has(completionId))
    ) {
      ids.push(completionId);
    }
  }
  return ids;
}

function normalizedMaxChecks(value: number | undefined): number | undefined {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, Math.trunc(value))
    : undefined;
}

function selectInflightTaskChecks(
  state: ChatState,
  opts: PollInflightOptions | undefined,
): { generationIds: string[]; completionIds: string[] } {
  const generationIds = selectableGenerationIds(
    state,
    opts?.generationIds ? new Set(opts.generationIds) : null,
  );
  const completionIds = selectableCompletionIds(
    state,
    opts?.completionIds ? new Set(opts.completionIds) : null,
  );
  const maxChecks = normalizedMaxChecks(opts?.maxChecks);
  if (maxChecks === undefined) return { generationIds, completionIds };
  const selectedGenerationIds = generationIds.slice(0, maxChecks);
  return {
    generationIds: selectedGenerationIds,
    completionIds: completionIds.slice(
      0,
      Math.max(0, maxChecks - selectedGenerationIds.length),
    ),
  };
}

function polledGenerationAttempt(
  fresh: BackendGeneration,
  fallback: number,
): number {
  return typeof fresh.attempt === "number" && Number.isFinite(fresh.attempt)
    ? fresh.attempt
    : fallback;
}

function terminalPolledGeneration(
  current: Generation,
  fresh: BackendGeneration,
  explainability: GenerationExplainabilityMeta,
  taskMeta: ReturnType<typeof generationTaskMetaFromBackend>,
): Generation {
  return {
    ...current,
    status: coerceGenerationStatus(fresh.status, current.status),
    stage: coerceGenerationStage(fresh.progress_stage, "finalizing"),
    attempt: polledGenerationAttempt(fresh, current.attempt),
    error_code: fresh.error_code ?? undefined,
    error_message: fresh.error_message ?? undefined,
    ...explainability,
    ...taskMeta,
    finished_at: fresh.finished_at ? isoToMs(fresh.finished_at) : Date.now(),
  };
}

function inflightPolledGeneration(
  current: Generation,
  fresh: BackendGeneration,
  explainability: GenerationExplainabilityMeta,
  taskMeta: ReturnType<typeof generationTaskMetaFromBackend>,
): Generation {
  return {
    ...current,
    status: coerceGenerationStatus(fresh.status, current.status),
    stage: coerceGenerationStage(fresh.progress_stage, current.stage),
    attempt: polledGenerationAttempt(fresh, current.attempt),
    error_code: fresh.error_code ?? undefined,
    error_message: fresh.error_message ?? undefined,
    ...explainability,
    ...taskMeta,
  };
}

function generationSnapshotChanged(
  current: Generation,
  incoming: Generation,
): boolean {
  return (
    incoming.status !== current.status ||
    incoming.stage !== current.stage ||
    incoming.attempt !== current.attempt ||
    incoming.error_code !== current.error_code ||
    incoming.error_message !== current.error_message
  );
}

function updatePolledGeneration(
  set: ChatStateSetter,
  generationId: string,
  fresh: BackendGeneration,
  explainability: GenerationExplainabilityMeta,
  taskMeta: ReturnType<typeof generationTaskMetaFromBackend>,
  terminal: boolean,
): void {
  set((state) => {
    const current = state.generations[generationId];
    if (!current || !isInflightGeneration(current)) return state;
    const incoming = terminal
      ? terminalPolledGeneration(current, fresh, explainability, taskMeta)
      : inflightPolledGeneration(current, fresh, explainability, taskMeta);
    if (!terminal && !generationSnapshotChanged(current, incoming)) {
      return state;
    }
    return {
      generations: {
        ...state.generations,
        [generationId]: incoming,
      },
    };
  });
}

function hasOwningGenerationMessage(
  state: ChatState,
  messageId: string,
): boolean {
  return state.messages.some(
    (message) => message.role === "assistant" && message.id === messageId,
  );
}

async function pollGenerationTask(
  generationId: string,
  opts: PollInflightOptions | undefined,
  get: ChatStateGetter,
  set: ChatStateSetter,
  dependencies: TaskRecoveryRuntime,
): Promise<TaskPollResult> {
  try {
    if (opts?.signal?.aborted) {
      return { status: "aborted", refetchConvId: null };
    }
    const fresh = await dependencies.getGenerationTask(generationId, {
      signal: opts?.signal,
    });
    const state = get();
    const local = state.generations[generationId];
    if (!local || !isInflightGeneration(local)) {
      return { status: "complete", refetchConvId: null };
    }
    const terminal = isTerminalTaskStatus(fresh.status);
    if (terminal && hasOwningGenerationMessage(state, fresh.message_id)) {
      return {
        status: "complete",
        refetchConvId: state.currentConvId,
      };
    }
    updatePolledGeneration(
      set,
      generationId,
      fresh,
      generationExplainabilityFromBackend(fresh),
      generationTaskMetaFromBackend(fresh),
      terminal,
    );
  } catch (error) {
    if (
      opts?.signal &&
      dependencies.isAbortRequest(error, opts.signal)
    ) {
      return { status: "aborted", refetchConvId: null };
    }
    logWarn("pollInflightTasks generation check failed", {
      scope: "chat-poll",
      code: error instanceof ApiError ? error.code : undefined,
      extra: {
        generationId,
        err: dependencies.errorToMessage(error),
      },
    });
    return { status: "failed", refetchConvId: null, error };
  }
  return { status: "complete", refetchConvId: null };
}

function owningCompletionMessage(
  state: ChatState,
  completionId: string,
): AssistantMessage | undefined {
  return state.messages.find(
    (message): message is AssistantMessage =>
      message.role === "assistant" &&
      message.completion_id === completionId,
  );
}

async function pollCompletionTask(
  completionId: string,
  opts: PollInflightOptions | undefined,
  get: ChatStateGetter,
  set: ChatStateSetter,
  dependencies: TaskRecoveryRuntime,
): Promise<TaskPollResult> {
  try {
    if (opts?.signal?.aborted) {
      return { status: "aborted", refetchConvId: null };
    }
    const fresh = await dependencies.getCompletionTask(completionId, {
      signal: opts?.signal,
    });
    dependencies.flushCompletionStreamPatches();
    const stateBeforeSnapshot = get();
    const owningMessageBeforeSnapshot = owningCompletionMessage(
      stateBeforeSnapshot,
      completionId,
    );
    const terminalHistoryConvId =
      owningMessageBeforeSnapshot &&
      isInflightAssistant(owningMessageBeforeSnapshot) &&
      isTerminalTaskStatus(fresh.status)
        ? stateBeforeSnapshot.currentConvId
        : null;
    const snapshotNow = Date.now();
    set((state) => ({
      messages: applyCompletionSnapshot(
        state.messages,
        completionId,
        fresh,
        snapshotNow,
      ),
    }));
    return { status: "complete", refetchConvId: terminalHistoryConvId };
  } catch (error) {
    if (
      opts?.signal &&
      dependencies.isAbortRequest(error, opts.signal)
    ) {
      return { status: "aborted", refetchConvId: null };
    }
    logWarn("pollInflightTasks completion check failed", {
      scope: "chat-poll",
      code: error instanceof ApiError ? error.code : undefined,
      extra: {
        completionId,
        err: dependencies.errorToMessage(error),
      },
    });
    return { status: "failed", refetchConvId: null, error };
  }
}

function activeCompletionIdentityError(
  completion: BackendCompletion,
): Error | null {
  if (
    !completion.id ||
    !completion.message_id ||
    !completion.conversation_id
  ) {
    return new Error(
      `active completion ${completion.id || "unknown"} lacks ownership identity`,
    );
  }
  return null;
}

function registerAndApplyActiveCompletions(
  messages: ChatState["messages"],
  completions: BackendCompletion[],
): ChatState["messages"] | Error {
  let registered = messages;
  for (const completion of completions) {
    const owner = registered.find(
      (message): message is AssistantMessage =>
        message.role === "assistant" && message.id === completion.message_id,
    );
    if (!owner) {
      return new Error(
        `active completion ${completion.id} has no owning message`,
      );
    }
    if (owner.completion_id && owner.completion_id !== completion.id) {
      return new Error(
        `active completion ${completion.id} conflicts with owning message`,
      );
    }
    if (!owner.completion_id) {
      registered = registered.map((message) =>
        message.role === "assistant" && message.id === owner.id
          ? { ...message, completion_id: completion.id }
          : message,
      );
    }
    registered = applyCompletionSnapshot(
      registered,
      completion.id,
      completion,
      Date.now(),
    );
  }
  return registered;
}

function hydrateSnapshotIsCurrent(
  state: ChatState,
  context: HydrateSnapshotContext,
  currentConversationId: string | null,
): boolean {
  return (
    !context.signal?.aborted &&
    context.dependencies.userSessionFence.isCurrent(context.userFence) &&
    state.currentUserId === context.requestedUserId &&
    state.currentConvId === currentConversationId
  );
}

function mergeHydratedTaskSnapshot(
  state: ChatState,
  context: HydrateSnapshotContext,
  currentConversationId: string | null,
  messages: ChatState["messages"],
  incoming: BackendGeneration[],
): Partial<ChatState> | ChatState {
  if (!hydrateSnapshotIsCurrent(state, context, currentConversationId)) {
    return state;
  }
  const generations = mergeUnknownActiveGenerations(
    state.generations,
    incoming,
  );
  return {
    messages,
    ...(generations ? { generations } : {}),
  };
}

function activeTaskIdentityFailure(
  completions: BackendCompletion[],
): TaskRecoveryOutcome | null {
  for (const completion of completions) {
    const error = activeCompletionIdentityError(completion);
    if (error) return { status: "failed", error };
  }
  return null;
}

function hydrateUserIsCurrent(context: HydrateSnapshotContext): boolean {
  const { userSessionFence } = context.dependencies;
  const { userFence } = context;
  return (
    !context.signal?.aborted &&
    userSessionFence.isCurrent(userFence) &&
    context.get().currentUserId === context.requestedUserId
  );
}

function completionOwnerIsMissing(
  messages: ChatState["messages"],
  completions: BackendCompletion[],
): boolean {
  const ownerIds = new Set(
    messages
      .filter((message) => message.role === "assistant")
      .map((message) => message.id),
  );
  return completions.some(
    (completion) => !ownerIds.has(completion.message_id),
  );
}

async function loadMissingCompletionOwners(
  currentConversationId: string | null,
  currentCompletions: BackendCompletion[],
  context: HydrateSnapshotContext,
): Promise<TaskRecoveryOutcome | null> {
  if (
    !currentConversationId ||
    !completionOwnerIsMissing(context.get().messages, currentCompletions)
  ) {
    return null;
  }
  try {
    await context.get().loadHistoricalMessages(currentConversationId);
    return null;
  } catch (error) {
    return { status: "failed", error };
  }
}

async function hydrateActiveTaskSnapshot(
  response: Awaited<ReturnType<typeof apiListMyActiveTasks>>,
  context: HydrateSnapshotContext,
): Promise<TaskRecoveryOutcome> {
  const completions = response.completions ?? [];
  const identityFailure = activeTaskIdentityFailure(completions);
  if (identityFailure) return identityFailure;
  if (!hydrateUserIsCurrent(context)) return ABORTED_RECOVERY;

  const currentConversationId = context.get().currentConvId;
  const currentCompletions = completions.filter(
    (completion) => completion.conversation_id === currentConversationId,
  );
  const loadFailure = await loadMissingCompletionOwners(
    currentConversationId,
    currentCompletions,
    context,
  );
  if (loadFailure) return loadFailure;
  if (
    !hydrateSnapshotIsCurrent(
      context.get(),
      context,
      currentConversationId,
    )
  ) {
    return ABORTED_RECOVERY;
  }
  const messages = registerAndApplyActiveCompletions(
    context.get().messages,
    currentCompletions,
  );
  if (messages instanceof Error) {
    return { status: "failed", error: messages };
  }
  const incoming = response.generations ?? [];
  context.set((state) =>
    mergeHydratedTaskSnapshot(
      state,
      context,
      currentConversationId,
      messages,
      incoming,
    ),
  );
  return COMPLETE_RECOVERY;
}

function pollFailureOutcome(
  results: TaskPollResult[],
): TaskRecoveryOutcome | null {
  const failed = results.find(
    (result): result is Extract<TaskPollResult, { status: "failed" }> =>
      result.status === "failed",
  );
  if (failed) return { status: "failed", error: failed.error };
  if (results.some((result) => result.status === "aborted")) {
    return ABORTED_RECOVERY;
  }
  return null;
}

function refetchConversationId(results: TaskPollResult[]): string | null {
  return (
    results.find(
      (result) =>
        result.status === "complete" && Boolean(result.refetchConvId),
    )?.refetchConvId ?? null
  );
}

async function refetchPolledConversation(
  conversationId: string | null,
  opts: PollInflightOptions | undefined,
  get: ChatStateGetter,
  dependencies: TaskRecoveryRuntime,
): Promise<TaskRecoveryOutcome | null> {
  if (!conversationId || opts?.signal?.aborted) return null;
  try {
    await get().loadHistoricalMessages(conversationId);
    return null;
  } catch (error) {
    logWarn("pollInflightTasks refetch failed", {
      scope: "chat-poll",
      code: error instanceof ApiError ? error.code : undefined,
      extra: {
        convId: conversationId,
        err: dependencies.errorToMessage(error),
      },
    });
    return { status: "failed", error };
  }
}

async function pollInflightTaskSnapshot(
  opts: PollInflightOptions | undefined,
  get: ChatStateGetter,
  set: ChatStateSetter,
  dependencies: TaskRecoveryRuntime,
): Promise<TaskRecoveryOutcome> {
  const checks = selectInflightTaskChecks(get(), opts);
  if (
    checks.generationIds.length === 0 &&
    checks.completionIds.length === 0
  ) {
    return COMPLETE_RECOVERY;
  }
  const results = await Promise.all([
    ...checks.generationIds.map((generationId) =>
      pollGenerationTask(generationId, opts, get, set, dependencies),
    ),
    ...checks.completionIds.map((completionId) =>
      pollCompletionTask(completionId, opts, get, set, dependencies),
    ),
  ]);
  const failed = pollFailureOutcome(results);
  if (failed) return failed;
  const refetchFailure = await refetchPolledConversation(
    refetchConversationId(results),
    opts,
    get,
    dependencies,
  );
  if (refetchFailure) return refetchFailure;
  return opts?.signal?.aborted ? ABORTED_RECOVERY : COMPLETE_RECOVERY;
}

export function createTaskRecoveryActions(
  set: ChatStateSetter,
  get: ChatStateGetter,
  inputDependencies: TaskRecoveryDependencies,
): TaskRecoveryActions {
  const dependencies = withDefaultApis(inputDependencies);
  const hydrateRequests = new Map<string, ActiveTaskHydrateRequest>();
  return {
    async refreshCompletionText(completionId, opts) {
      try {
        const fresh = await dependencies.getCompletionTask(completionId, {
          signal: opts?.signal,
        });
        dependencies.flushCompletionStreamPatches();
        const snapshotNow = Date.now();
        set((state) => ({
          messages: applyCompletionSnapshot(
            state.messages,
            completionId,
            fresh,
            snapshotNow,
          ),
        }));
      } catch (error) {
        if (
          opts?.signal &&
          dependencies.isAbortRequest(error, opts.signal)
        ) {
          return;
        }
        logWarn("refreshCompletionText failed", {
          scope: "chat-poll",
          code: error instanceof ApiError ? error.code : undefined,
          extra: {
            completionId,
            err: dependencies.errorToMessage(error),
          },
        });
        throw error;
      }
    },

    async pollInflightTasks(opts) {
      return pollInflightTaskSnapshot(opts, get, set, dependencies);
    },

    async hydrateActiveTasks(opts) {
      if (opts?.signal?.aborted) return ABORTED_RECOVERY;
      const requestedUserId = get().currentUserId;
      if (requestedUserId === null) return COMPLETE_RECOVERY;
      const userFence = dependencies.userSessionFence.snapshot();
      const requestKey = JSON.stringify([userFence, requestedUserId]);
      const existing = hydrateRequests.get(requestKey);
      if (existing && !existing.signal?.aborted) return existing.promise;

      const request: ActiveTaskHydrateRequest = {
        promise: Promise.resolve(COMPLETE_RECOVERY),
        signal: opts?.signal,
      };
      request.promise = (async () => {
        let response: Awaited<ReturnType<typeof apiListMyActiveTasks>>;
        try {
          response = await dependencies.listActiveTasks({
            signal: opts?.signal,
          });
        } catch (error) {
          if (
            opts?.signal &&
            dependencies.isAbortRequest(error, opts.signal)
          ) {
            return ABORTED_RECOVERY;
          }
          logWarn("hydrateActiveTasks fetch failed", {
            scope: "chat-hydrate",
            code: error instanceof ApiError ? error.code : undefined,
            extra: { err: dependencies.errorToMessage(error) },
          });
          return { status: "failed" as const, error };
        }
        return hydrateActiveTaskSnapshot(response, {
          requestedUserId,
          userFence,
          signal: opts?.signal,
          get,
          set,
          dependencies,
        });
      })().finally(() => {
        if (hydrateRequests.get(requestKey) === request) {
          hydrateRequests.delete(requestKey);
        }
      });
      hydrateRequests.set(requestKey, request);
      return request.promise;
    },
  };
}
