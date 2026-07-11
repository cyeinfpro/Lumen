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
} from "../chatGenerationEvents";
import {
  generationExplainabilityFromBackend,
  generationTaskMetaFromBackend,
  isInflightGeneration,
  mergeUnknownActiveGenerations,
  type GenerationExplainabilityMeta,
} from "./generationSlice";
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
  promise: Promise<void>;
  signal?: AbortSignal;
};

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
): Promise<string | null> {
  try {
    if (opts?.signal?.aborted) return null;
    const fresh = await dependencies.getGenerationTask(generationId, {
      signal: opts?.signal,
    });
    const state = get();
    const local = state.generations[generationId];
    if (!local || !isInflightGeneration(local)) return null;
    const terminal = isTerminalTaskStatus(fresh.status);
    if (terminal && hasOwningGenerationMessage(state, fresh.message_id)) {
      return state.currentConvId;
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
      return null;
    }
    logWarn("pollInflightTasks generation check failed", {
      scope: "chat-poll",
      code: error instanceof ApiError ? error.code : undefined,
      extra: {
        generationId,
        err: dependencies.errorToMessage(error),
      },
    });
  }
  return null;
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
): Promise<string | null> {
  try {
    if (opts?.signal?.aborted) return null;
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
    return terminalHistoryConvId;
  } catch (error) {
    if (
      opts?.signal &&
      dependencies.isAbortRequest(error, opts.signal)
    ) {
      return null;
    }
    logWarn("pollInflightTasks completion check failed", {
      scope: "chat-poll",
      code: error instanceof ApiError ? error.code : undefined,
      extra: {
        completionId,
        err: dependencies.errorToMessage(error),
      },
    });
    return null;
  }
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
      const checks = selectInflightTaskChecks(get(), opts);
      if (
        checks.generationIds.length === 0 &&
        checks.completionIds.length === 0
      ) {
        return;
      }
      const refetchCandidates = await Promise.all([
        ...checks.generationIds.map((generationId) =>
          pollGenerationTask(generationId, opts, get, set, dependencies),
        ),
        ...checks.completionIds.map((completionId) =>
          pollCompletionTask(completionId, opts, get, set, dependencies),
        ),
      ]);
      const needRefetchConvId =
        refetchCandidates.find((convId): convId is string => Boolean(convId)) ??
        null;
      if (needRefetchConvId && !opts?.signal?.aborted) {
        try {
          await get().loadHistoricalMessages(needRefetchConvId);
        } catch (error) {
          logWarn("pollInflightTasks refetch failed", {
            scope: "chat-poll",
            code: error instanceof ApiError ? error.code : undefined,
            extra: {
              convId: needRefetchConvId,
              err: dependencies.errorToMessage(error),
            },
          });
        }
      }
    },

    async hydrateActiveTasks(opts) {
      if (opts?.signal?.aborted) return;
      const requestedUserId = get().currentUserId;
      if (requestedUserId === null) return;
      const userFence = dependencies.userSessionFence.snapshot();
      const requestKey = JSON.stringify([userFence, requestedUserId]);
      const existing = hydrateRequests.get(requestKey);
      if (existing && !existing.signal?.aborted) return existing.promise;

      const request: ActiveTaskHydrateRequest = {
        promise: Promise.resolve(),
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
            return;
          }
          logWarn("hydrateActiveTasks fetch failed", {
            scope: "chat-hydrate",
            code: error instanceof ApiError ? error.code : undefined,
            extra: { err: dependencies.errorToMessage(error) },
          });
          return;
        }
        if (
          opts?.signal?.aborted ||
          !dependencies.userSessionFence.isCurrent(userFence) ||
          get().currentUserId !== requestedUserId
        ) {
          return;
        }
        const incoming = response.generations ?? [];
        if (incoming.length === 0) return;
        set((state) => {
          if (
            opts?.signal?.aborted ||
            !dependencies.userSessionFence.isCurrent(userFence) ||
            state.currentUserId !== requestedUserId
          ) {
            return state;
          }
          const generations = mergeUnknownActiveGenerations(
            state.generations,
            incoming,
          );
          return generations ? { generations } : state;
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
