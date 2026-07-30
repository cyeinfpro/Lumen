import {
  getVideoAssetOperation,
  retryVideoAssetOperation,
} from "@/lib/apiClient";
import {
  VOLCANO_OPERATION_POLL_TIMEOUT_MS,
  volcanoAssetErrorMessage,
  volcanoCreateFailureRecovery,
  volcanoOperationBlocksMutation,
  volcanoOperationIsRetryable,
  volcanoOperationStartedAt,
  volcanoOperationStatusKind,
  volcanoOperationTimedOut,
} from "./volcano-asset-domain";
import { abortableDelay } from "./volcano-asset-manager-helpers";
import type {
  OperationItem,
  OperationRunner,
} from "./volcano-asset-manager-types";
import { POLL_INTERVAL_MS } from "./volcano-asset-manager-types";
import { isAbortError } from "./video-request-lifecycle";

interface RunVolcanoOperationOptions {
  clientOperationId: string;
  controllers: Map<string, AbortController>;
  queues: Map<string, OperationItem[]>;
  runners: Map<string, OperationRunner>;
  locks: Map<string, string>;
  isSessionActive: (sessionId: number, expectedModel?: string) => boolean;
  updateOperation: (
    id: string,
    patch: Partial<OperationItem>,
    queueModel?: string,
  ) => void;
}

export async function runVolcanoOperation({
  clientOperationId,
  controllers,
  queues,
  runners,
  locks,
  isSessionActive,
  updateOperation,
}: RunVolcanoOperationOptions): Promise<void> {
  const runner = runners.get(clientOperationId);
  if (
    !runner ||
    !isSessionActive(runner.sessionId, runner.model) ||
    controllers.has(clientOperationId)
  ) {
    return;
  }
  const operationItem = (queues.get(runner.model) ?? []).find(
    (item) => item.id === clientOperationId,
  );
  if (!operationItem) return;

  const controller = new AbortController();
  const sessionId = runner.sessionId;
  let remoteOperationId = operationItem.remoteOperationId;
  let submissionStartedAt = operationItem.submissionStartedAt;
  controllers.set(clientOperationId, controller);
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
    const operationStartedAtWasMissing =
      operationItem.operationStartedAt == null;
    const operationStartedAt = volcanoOperationStartedAt(
      operationItem.operationStartedAt,
    );
    const operationPatch: Partial<OperationItem> = {
      remoteOperationId,
      progressStage: operation.progress_stage,
      retryAfterSeconds: operation.retry_after_seconds,
      recovery: "resume",
    };
    if (operationStartedAtWasMissing) {
      operationPatch.operationStartedAt = operationStartedAt;
    }
    updateOperation(clientOperationId, operationPatch, runner.model);

    let pollFailures = 0;
    while (
      !controller.signal.aborted &&
      isSessionActive(sessionId, runner.model)
    ) {
      runner.onProgress?.(operation, sessionId, operationStartedAt);
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
    if (controllers.get(clientOperationId) === controller) {
      controllers.delete(clientOperationId);
    }
    const current = (queues.get(runner.model) ?? []).find(
      (item) => item.id === clientOperationId,
    );
    if (
      current &&
      !volcanoOperationBlocksMutation(current) &&
      locks.get(runner.lockKey) === clientOperationId
    ) {
      locks.delete(runner.lockKey);
    }
  }
}
