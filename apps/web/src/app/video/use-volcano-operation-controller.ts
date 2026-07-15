"use client";

/* eslint complexity: "off" */

import {
  useCallback,
  useRef,
  useState,
  type RefObject,
} from "react";

import {
  getVideoAssetOperation,
  retryVideoAssetOperation,
} from "@/lib/apiClient";

import {
  VOLCANO_OPERATION_POLL_TIMEOUT_MS,
  pauseVolcanoOperationCheckpoints,
  volcanoAssetErrorMessage,
  volcanoCreateFailureRecovery,
  volcanoOperationBlocksMutation,
  volcanoOperationIsRetryable,
  volcanoOperationLocksConflict,
  volcanoOperationStatusKind,
  volcanoOperationTimedOut,
} from "./volcano-asset-domain";
import {
  abortableDelay,
  clientId,
} from "./volcano-asset-manager-helpers";
import type {
  ActiveSession,
  Notice,
  OperationItem,
  OperationRunner,
} from "./volcano-asset-manager-types";
import { POLL_INTERVAL_MS } from "./volcano-asset-manager-types";
import { isAbortError } from "./video-request-lifecycle";

type OperationInput = Omit<
  OperationItem,
  "id" | "model" | "phase" | "recovery" | "pollFailures" | "retryable"
>;

type OperationRunnerInput = Omit<
  OperationRunner,
  "model" | "lockKey" | "sessionId"
>;

export type VolcanoOperationController = {
  operations: OperationItem[];
  operationsRef: RefObject<OperationItem[]>;
  operationQueuesRef: RefObject<Map<string, OperationItem[]>>;
  enqueueOperation: (
    input: OperationInput,
    runnerInput: OperationRunnerInput,
  ) => string | null;
  retryOperation: (clientOperationId: string) => void;
  dismissOperation: (clientOperationId: string) => void;
  retireOperation: (clientOperationId: string) => boolean;
  getOperation: (
    clientOperationId: string,
    queueModel?: string,
  ) => OperationItem | undefined;
  operationHasConflict: (
    lockKey: string,
    ignoredOperationId?: string,
  ) => boolean;
  abortOperationRequests: () => void;
  restoreOperationQueue: (
    queueModel: string,
    sessionId: number,
  ) => OperationItem[];
  pauseActiveOperationQueue: (queueModel: string) => OperationItem[];
  showOperations: (items: OperationItem[]) => void;
  resumePausedOperations: (items: OperationItem[]) => void;
};

export function useVolcanoOperationController({
  activeSessionRef,
  isSessionActive,
  setNotice,
}: {
  activeSessionRef: RefObject<ActiveSession>;
  isSessionActive: (sessionId: number, expectedModel?: string) => boolean;
  setNotice: (notice: Notice) => void;
}): VolcanoOperationController {
  const operationControllersRef = useRef(
    new Map<string, AbortController>(),
  );
  const operationsRef = useRef<OperationItem[]>([]);
  const operationQueuesRef = useRef(new Map<string, OperationItem[]>());
  const operationRunnersRef = useRef(new Map<string, OperationRunner>());
  const operationLocksRef = useRef(new Map<string, string>());
  const [operations, setOperations] = useState<OperationItem[]>([]);

  const commitOperationQueue = useCallback(
    (
      queueModel: string,
      updater: (current: OperationItem[]) => OperationItem[],
    ): OperationItem[] => {
      const current =
        operationQueuesRef.current.get(queueModel) ??
        (activeSessionRef.current.model === queueModel
          ? operationsRef.current
          : []);
      const next = updater(current);
      operationQueuesRef.current.set(queueModel, next);
      const active = activeSessionRef.current;
      if (active.model === queueModel) {
        operationsRef.current = next;
        if (active.open) setOperations(next);
      }
      return next;
    },
    [activeSessionRef],
  );

  const updateOperation = useCallback(
    (id: string, patch: Partial<OperationItem>, queueModel?: string): void => {
      let resolvedModel = queueModel;
      if (!resolvedModel) {
        for (const [candidateModel, queue] of operationQueuesRef.current) {
          if (queue.some((item) => item.id === id)) {
            resolvedModel = candidateModel;
            break;
          }
        }
      }
      resolvedModel ??= activeSessionRef.current.model;
      commitOperationQueue(resolvedModel, (current) =>
        current.map((item) => (item.id === id ? { ...item, ...patch } : item)),
      );
    },
    [activeSessionRef, commitOperationQueue],
  );

  const getOperation = useCallback(
    (clientOperationId: string, queueModel?: string) => {
      if (queueModel) {
        return operationQueuesRef.current
          .get(queueModel)
          ?.find((item) => item.id === clientOperationId);
      }
      for (const queue of operationQueuesRef.current.values()) {
        const operation = queue.find(
          (item) => item.id === clientOperationId,
        );
        if (operation) return operation;
      }
      return undefined;
    },
    [],
  );

  const operationHasConflict = useCallback(
    (lockKey: string, ignoredOperationId?: string): boolean => {
      return Array.from(operationLocksRef.current.entries()).some(
        ([current, operationId]) =>
          operationId !== ignoredOperationId &&
          volcanoOperationLocksConflict(current, lockKey),
      );
    },
    [],
  );

  const runOperation = useCallback(
    async (clientOperationId: string) => {
      const runner = operationRunnersRef.current.get(clientOperationId);
      if (
        !runner ||
        !isSessionActive(runner.sessionId, runner.model) ||
        operationControllersRef.current.has(clientOperationId)
      ) {
        return;
      }
      const operationItem = (
        operationQueuesRef.current.get(runner.model) ?? []
      ).find((item) => item.id === clientOperationId);
      if (!operationItem) return;

      const controller = new AbortController();
      const sessionId = runner.sessionId;
      let remoteOperationId = operationItem.remoteOperationId;
      let submissionStartedAt = operationItem.submissionStartedAt;
      operationControllersRef.current.set(clientOperationId, controller);
      updateOperation(
        clientOperationId,
        {
          phase: "pending",
          error: undefined,
          pollFailures: 0,
        },
        runner.model,
      );

      const pauseForUnknownResult = (message: string) => {
        const submissionMayHaveStarted = Boolean(submissionStartedAt);
        const canResume = Boolean(remoteOperationId) || !submissionMayHaveStarted;
        updateOperation(
          clientOperationId,
          {
            phase: canResume ? "paused" : "uncertain",
            recovery: canResume ? "resume" : "refresh",
            remoteOperationId,
            submissionStartedAt,
            error: message,
          },
          runner.model,
        );
        if (remoteOperationId || submissionMayHaveStarted) {
          runner.onUncertain?.(message, sessionId);
        }
      };

      try {
        let operation;
        if (operationItem.recovery === "retry" && remoteOperationId) {
          submissionStartedAt = Date.now();
          updateOperation(
            clientOperationId,
            { submissionStartedAt },
            runner.model,
          );
          try {
            operation = await retryVideoAssetOperation(remoteOperationId, {
              signal: controller.signal,
            });
          } catch (error) {
            if (
              isAbortError(error) ||
              controller.signal.aborted ||
              !isSessionActive(sessionId, runner.model)
            ) {
              pauseForUnknownResult(
                "重试请求已发出但结果未确认。重新打开后会查询原任务，不会再次发送重试。",
              );
              return;
            }
            if (volcanoCreateFailureRecovery(error) === "verify") {
              pauseForUnknownResult(
                `${volcanoAssetErrorMessage(
                  error,
                  "重试请求结果未知",
                )}。请查询原任务状态，系统不会再次发送重试。`,
              );
              return;
            }
            throw error;
          }
        } else if (remoteOperationId) {
          operation = await getVideoAssetOperation(remoteOperationId, {
            signal: controller.signal,
          });
        } else {
          await runner.prepare?.(controller.signal);
          submissionStartedAt = Date.now();
          updateOperation(
            clientOperationId,
            { submissionStartedAt },
            runner.model,
          );
          try {
            operation = await runner.submit(controller.signal);
          } catch (error) {
            if (
              isAbortError(error) ||
              controller.signal.aborted ||
              !isSessionActive(sessionId, runner.model)
            ) {
              pauseForUnknownResult(
                "提交请求已发出但结果未知。系统不会自动重发，请检查素材库后再继续。",
              );
              return;
            }
            const recovery = volcanoCreateFailureRecovery(error);
            if (recovery === "verify") {
              pauseForUnknownResult(
                `${volcanoAssetErrorMessage(
                  error,
                  "后台操作提交结果未知",
                )}。系统不会自动重发，请先检查素材库。`,
              );
              return;
            }
            throw error;
          }
        }

        remoteOperationId = operation.id.trim() || remoteOperationId;
        if (!remoteOperationId) {
          pauseForUnknownResult(
            "后台已接收请求但没有返回任务标识。请检查素材库，系统不会重复提交。",
          );
          return;
        }
        if (operation.id !== remoteOperationId) {
          operation = { ...operation, id: remoteOperationId };
        }
        const operationStartedAt =
          operationItem.operationStartedAt ?? Date.now();
        updateOperation(
          clientOperationId,
          {
            remoteOperationId,
            operationStartedAt,
            progressStage: operation.progress_stage,
            retryAfterSeconds: operation.retry_after_seconds,
            recovery: "resume",
          },
          runner.model,
        );

        let pollFailures = 0;
        while (
          !controller.signal.aborted &&
          isSessionActive(sessionId, runner.model)
        ) {
          runner.onProgress?.(operation, sessionId);
          const statusKind = volcanoOperationStatusKind(operation.status);
          if (statusKind === "succeeded") {
            if (!operation.result) {
              pauseForUnknownResult(
                "后台任务已完成但结果暂不可用。请检查状态，系统不会重复提交。",
              );
              return;
            }
            await runner.onSucceeded(operation.result, operation, sessionId);
            updateOperation(
              clientOperationId,
              {
                phase: "succeeded",
                recovery: "none",
                retryable: false,
                retryAfterSeconds: null,
                retryAvailableAt: undefined,
                progressStage: operation.progress_stage,
                pollFailures: 0,
                error: undefined,
              },
              runner.model,
            );
            return;
          }
          if (statusKind === "failed") {
            const retryable = volcanoOperationIsRetryable(operation);
            const retryAfterSeconds =
              operation.retry_after_seconds ??
              operation.error?.retry_after_seconds ??
              null;
            updateOperation(
              clientOperationId,
              {
                phase: "failed",
                recovery: retryable ? "retry" : "none",
                retryable,
                retryAfterSeconds,
                retryAvailableAt:
                  retryable && retryAfterSeconds
                    ? Date.now() + retryAfterSeconds * 1000
                    : undefined,
                progressStage: operation.progress_stage,
                pollFailures: 0,
                error: volcanoAssetErrorMessage(
                  operation.error,
                  "后台操作失败",
                ),
              },
              runner.model,
            );
            runner.onFailed?.(operation, sessionId);
            return;
          }
          if (statusKind === "unknown") {
            pauseForUnknownResult(
              "后台返回了未知任务状态。请检查状态，系统不会重复提交。",
            );
            return;
          }
          if (
            volcanoOperationTimedOut(
              operationStartedAt,
              Date.now(),
              VOLCANO_OPERATION_POLL_TIMEOUT_MS,
            )
          ) {
            pauseForUnknownResult(
              "后台处理时间较长，已暂停自动轮询。点“检查状态”可继续确认。",
            );
            return;
          }

          updateOperation(
            clientOperationId,
            {
              phase: "pending",
              recovery: "resume",
              progressStage: operation.progress_stage,
              retryable: operation.retryable,
              retryAfterSeconds: operation.retry_after_seconds,
              pollFailures,
              error: undefined,
            },
            runner.model,
          );
          await abortableDelay(POLL_INTERVAL_MS, controller.signal);
          try {
            operation = await getVideoAssetOperation(remoteOperationId, {
              signal: controller.signal,
            });
            pollFailures = 0;
          } catch (error) {
            if (isAbortError(error) || controller.signal.aborted) throw error;
            pollFailures += 1;
            if (pollFailures >= 3) {
              pauseForUnknownResult(
                `${volcanoAssetErrorMessage(
                  error,
                  "后台状态刷新失败",
                )}。点“检查状态”可继续确认，不会重新提交。`,
              );
              return;
            }
            updateOperation(
              clientOperationId,
              {
                pollFailures,
                error: `状态刷新暂时失败，将自动重试（${pollFailures}/3）`,
              },
              runner.model,
            );
          }
        }
      } catch (error) {
        if (
          isAbortError(error) ||
          controller.signal.aborted ||
          !isSessionActive(sessionId, runner.model)
        ) {
          pauseForUnknownResult(
            remoteOperationId
              ? "状态轮询已暂停，重新打开后会继续确认后台结果。"
              : submissionStartedAt
                ? "提交请求已发出但结果未知。系统不会自动重发，请检查素材库后再继续。"
                : "后台操作已暂停，重新打开后会继续。",
          );
          return;
        }
        if (remoteOperationId) {
          pauseForUnknownResult(
            `${volcanoAssetErrorMessage(
              error,
              "后台任务状态读取失败",
            )}。请检查状态，系统不会重复提交。`,
          );
          return;
        }
        updateOperation(
          clientOperationId,
          {
            phase: "failed",
            recovery: "none",
            retryable: false,
            error: volcanoAssetErrorMessage(error, "后台操作失败"),
          },
          runner.model,
        );
        runner.onSubmissionFailed?.(error, sessionId);
      } finally {
        if (
          operationControllersRef.current.get(clientOperationId) === controller
        ) {
          operationControllersRef.current.delete(clientOperationId);
        }
        const current = (
          operationQueuesRef.current.get(runner.model) ?? []
        ).find((item) => item.id === clientOperationId);
        if (
          current &&
          !volcanoOperationBlocksMutation(current) &&
          operationLocksRef.current.get(runner.lockKey) === clientOperationId
        ) {
          operationLocksRef.current.delete(runner.lockKey);
        }
      }
    },
    [isSessionActive, updateOperation],
  );

  const enqueueOperation = useCallback(
    (
      input: OperationInput,
      runnerInput: OperationRunnerInput,
    ): string | null => {
      const session = activeSessionRef.current;
      if (!session.open || operationHasConflict(input.lockKey)) return null;
      const id = clientId();
      const item: OperationItem = {
        ...input,
        id,
        model: session.model,
        phase: "pending",
        recovery: "resume",
        pollFailures: 0,
        retryable: false,
      };
      operationLocksRef.current.set(input.lockKey, id);
      operationRunnersRef.current.set(id, {
        ...runnerInput,
        model: session.model,
        lockKey: input.lockKey,
        sessionId: session.id,
      });
      commitOperationQueue(session.model, (current) => [item, ...current]);
      queueMicrotask(() => void runOperation(id));
      return id;
    },
    [
      activeSessionRef,
      commitOperationQueue,
      operationHasConflict,
      runOperation,
    ],
  );

  const retryOperation = useCallback(
    (clientOperationId: string) => {
      const runner = operationRunnersRef.current.get(clientOperationId);
      if (!runner || !isSessionActive(runner.sessionId, runner.model)) return;
      const item = getOperation(clientOperationId, runner.model);
      if (!item) return;
      if (item.retryAvailableAt != null && item.retryAvailableAt > Date.now()) {
        const seconds = Math.max(
          1,
          Math.ceil((item.retryAvailableAt - Date.now()) / 1000),
        );
        setNotice({
          tone: "error",
          text: `后台任务仍在冷却，请 ${seconds} 秒后重试`,
        });
        return;
      }
      if (operationHasConflict(runner.lockKey, clientOperationId)) {
        setNotice({
          tone: "error",
          text: "该对象或所属素材组已有后台操作进行中",
        });
        return;
      }

      if (item.recovery === "refresh" && !item.remoteOperationId) {
        const controller = new AbortController();
        operationControllersRef.current.set(clientOperationId, controller);
        updateOperation(
          clientOperationId,
          { phase: "pending", error: "正在刷新素材库确认结果" },
          runner.model,
        );
        void (async () => {
          try {
            const resolved =
              (await runner.verifyUnknown?.(
                controller.signal,
                runner.sessionId,
              )) ?? false;
            updateOperation(
              clientOperationId,
              resolved
                ? {
                    phase: "succeeded",
                    recovery: "none",
                    error: undefined,
                  }
                : {
                    phase: "uncertain",
                    recovery: "refresh",
                    error:
                      "仍无法确认提交结果。请检查素材列表；系统不会自动重发。",
                  },
              runner.model,
            );
            if (
              resolved &&
              operationLocksRef.current.get(runner.lockKey) ===
                clientOperationId
            ) {
              operationLocksRef.current.delete(runner.lockKey);
            }
          } catch (error) {
            if (!isAbortError(error) && !controller.signal.aborted) {
              updateOperation(
                clientOperationId,
                {
                  phase: "uncertain",
                  recovery: "refresh",
                  error: `${volcanoAssetErrorMessage(
                    error,
                    "素材库刷新失败",
                  )}。系统不会自动重发。`,
                },
                runner.model,
              );
            }
          } finally {
            if (
              operationControllersRef.current.get(clientOperationId) ===
              controller
            ) {
              operationControllersRef.current.delete(clientOperationId);
            }
          }
        })();
        return;
      }

      if (
        item.recovery !== "resume" &&
        !(item.recovery === "retry" && item.retryable)
      ) {
        return;
      }
      operationLocksRef.current.set(runner.lockKey, clientOperationId);
      updateOperation(
        clientOperationId,
        {
          phase: "pending",
          error: undefined,
        },
        runner.model,
      );
      void runOperation(clientOperationId);
    },
    [
      getOperation,
      isSessionActive,
      operationHasConflict,
      runOperation,
      setNotice,
      updateOperation,
    ],
  );

  const retireOperation = useCallback(
    (clientOperationId: string): boolean => {
      const runner = operationRunnersRef.current.get(clientOperationId);
      let queueModel = runner?.model;
      let operation = queueModel
        ? getOperation(clientOperationId, queueModel)
        : undefined;
      if (!queueModel || !operation) {
        for (const [candidateModel, queue] of operationQueuesRef.current) {
          const candidate = queue.find(
            (item) => item.id === clientOperationId,
          );
          if (candidate) {
            queueModel = candidateModel;
            operation = candidate;
            break;
          }
        }
      }
      if (operation && volcanoOperationBlocksMutation(operation)) {
        return false;
      }
      operationControllersRef.current.get(clientOperationId)?.abort();
      operationControllersRef.current.delete(clientOperationId);
      if (queueModel) {
        commitOperationQueue(queueModel, (current) =>
          current.filter((item) => item.id !== clientOperationId),
        );
      }
      for (const [lockKey, operationId] of operationLocksRef.current) {
        if (operationId === clientOperationId) {
          operationLocksRef.current.delete(lockKey);
        }
      }
      operationRunnersRef.current.delete(clientOperationId);
      return true;
    },
    [commitOperationQueue, getOperation],
  );

  const dismissOperation = useCallback(
    (clientOperationId: string) => {
      const operation = getOperation(clientOperationId);
      if (operation && volcanoOperationBlocksMutation(operation)) {
        setNotice({
          tone: "error",
          text: "该后台操作尚未确认完成，不能移除记录或释放对象锁",
        });
        return;
      }
      retireOperation(clientOperationId);
    },
    [getOperation, retireOperation, setNotice],
  );

  const abortOperationRequests = useCallback(() => {
    for (const controller of operationControllersRef.current.values()) {
      controller.abort();
    }
    operationControllersRef.current.clear();
  }, []);

  const restoreOperationQueue = useCallback(
    (queueModel: string, sessionId: number) => {
      operationLocksRef.current.clear();
      const restored = pauseVolcanoOperationCheckpoints(
        operationQueuesRef.current.get(queueModel) ?? [],
      );
      operationQueuesRef.current.set(queueModel, restored);
      operationsRef.current = restored;
      for (const item of restored) {
        const runner = operationRunnersRef.current.get(item.id);
        if (runner) runner.sessionId = sessionId;
        if (volcanoOperationBlocksMutation(item)) {
          operationLocksRef.current.set(item.lockKey, item.id);
        }
      }
      return restored;
    },
    [],
  );

  const pauseActiveOperationQueue = useCallback((queueModel: string) => {
    const paused = pauseVolcanoOperationCheckpoints(operationsRef.current);
    operationQueuesRef.current.set(queueModel, paused);
    operationsRef.current = paused;
    return paused;
  }, []);

  const showOperations = useCallback((items: OperationItem[]) => {
    operationsRef.current = items;
    setOperations(items);
  }, []);

  const resumePausedOperations = useCallback(
    (items: OperationItem[]) => {
      for (const item of items) {
        if (
          item.phase === "paused" &&
          item.recovery === "resume" &&
          operationRunnersRef.current.has(item.id)
        ) {
          void runOperation(item.id);
        }
      }
    },
    [runOperation],
  );

  return {
    operations,
    operationsRef,
    operationQueuesRef,
    enqueueOperation,
    retryOperation,
    dismissOperation,
    retireOperation,
    getOperation,
    operationHasConflict,
    abortOperationRequests,
    restoreOperationQueue,
    pauseActiveOperationQueue,
    showOperations,
    resumePausedOperations,
  };
}
