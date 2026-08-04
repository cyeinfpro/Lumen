"use client";

import { useCallback, useState } from "react";
import type { QueryClient } from "@tanstack/react-query";

import {
  qk,
  useRollbackPreviousMutation,
  useRollbackReleaseMutation,
  useTriggerAdminUpdateMutation,
} from "@/lib/queries";
import {
  ApiError,
  checkAdminUpdate,
  type AdminUpdateCheckOut,
  type AdminUpdateVersionOut,
} from "@/lib/apiClient";
import {
  anyPending,
  mutationErrorText,
  rollbackPendingIdFor,
  rollbackStartedBanner,
  setRunningUpdateStatus,
  triggerStartedText,
  type PendingUpdateConfirm,
  type UpdateBanner,
} from "./AdminUpdatePanel.helpers";

interface UseAdminUpdateActionsOptions {
  queryClient: QueryClient;
  updateVersion: AdminUpdateVersionOut | undefined;
}

export function useAdminUpdateActions({
  queryClient,
  updateVersion,
}: UseAdminUpdateActionsOptions) {
  const [updateBanner, setUpdateBanner] = useState<UpdateBanner | null>(null);
  const [updateStreamArmed, setUpdateStreamArmed] = useState(false);
  const [manualCheckPending, setManualCheckPending] = useState(false);
  const [pendingUpdateConfirm, setPendingUpdateConfirm] =
    useState<PendingUpdateConfirm | null>(null);

  const triggerUpdateMut = useTriggerAdminUpdateMutation({
    onSuccess: (result) => {
      setUpdateStreamArmed(true);
      setRunningUpdateStatus(queryClient, result.started_at, result);
      setUpdateBanner({
        kind: "success",
        text: triggerStartedText(result),
      });
    },
    onError: (error) => {
      setUpdateStreamArmed(false);
      const message = mutationErrorText(error, "触发更新失败");
      setUpdateBanner({
        kind: "error",
        text: `触发更新失败：${message}`,
      });
    },
  });

  const previousRollbackMut = useRollbackPreviousMutation({
    onSuccess: (result) => {
      setUpdateStreamArmed(true);
      setRunningUpdateStatus(queryClient, result.started_at);
      setUpdateBanner(rollbackStartedBanner(result, true));
    },
  });

  const rollbackMut = useRollbackReleaseMutation({
    onSuccess: (result) => {
      setUpdateStreamArmed(true);
      setRunningUpdateStatus(queryClient, result.started_at);
      setUpdateBanner(rollbackStartedBanner(result, false));
    },
    onError: (error) => {
      setUpdateStreamArmed(false);
      const message = mutationErrorText(error, "触发回滚失败");
      setUpdateBanner({
        kind: "error",
        text: `触发回滚失败：${message}`,
      });
    },
  });

  const runUpdateCheck = useCallback(
    async (force = false) => {
      setManualCheckPending(true);
      try {
        const data = await checkAdminUpdate(force);
        queryClient.setQueryData(qk.adminUpdateCheck(false), data);
        queryClient.setQueryData<AdminUpdateVersionOut | undefined>(
          qk.adminUpdateVersion(),
          {
            version: data.current_version,
            image_tag: updateVersion?.image_tag ?? `v${data.current_version}`,
            release_id: updateVersion?.release_id ?? null,
            sha: updateVersion?.sha ?? null,
            channel: data.channel,
            build_type: data.build_type,
            degraded: updateVersion?.degraded ?? [],
          },
        );
      } catch (error) {
        const message =
          error instanceof ApiError
            ? error.message
            : error instanceof Error
              ? error.message
              : "检查更新失败";
        setUpdateBanner({
          kind: "error",
          text: `检查更新失败：${message}`,
        });
      } finally {
        setManualCheckPending(false);
      }
    },
    [queryClient, updateVersion],
  );

  const requestUpdateConfirm = useCallback((check?: AdminUpdateCheckOut) => {
    const targetTag = check?.resolved_image_tag?.trim();
    if (!targetTag) {
      setUpdateBanner({
        kind: "error",
        text: "先重新检查更新，确认目标版本后再运行更新脚本。",
      });
      return;
    }
    setPendingUpdateConfirm({
      targetTag,
      channel: check?.channel ?? null,
    });
  }, []);

  const triggerConfirmedUpdate = useCallback(() => {
    if (!pendingUpdateConfirm) return;
    setUpdateBanner(null);
    triggerUpdateMut.mutate({
      target_tag: pendingUpdateConfirm.targetTag,
      channel: pendingUpdateConfirm.channel ?? undefined,
      force_redeploy: false,
      confirm_update: true,
      confirmed_target_tag: pendingUpdateConfirm.targetTag,
    });
    setPendingUpdateConfirm(null);
  }, [pendingUpdateConfirm, triggerUpdateMut]);

  const rollbackPrevious = useCallback(() => {
    setUpdateBanner(null);
    previousRollbackMut.mutate();
  }, [previousRollbackMut]);

  const rollbackRelease = useCallback(
    (releaseId: string) => {
      setUpdateBanner(null);
      rollbackMut.mutate(releaseId);
    },
    [rollbackMut],
  );

  const clearBanner = useCallback(() => setUpdateBanner(null), []);
  const closeUpdateConfirm = useCallback(
    () => setPendingUpdateConfirm(null),
    [],
  );
  const triggering = anyPending(
    triggerUpdateMut.isPending,
    rollbackMut.isPending,
    previousRollbackMut.isPending,
  );
  const rollbackPendingId = rollbackPendingIdFor(
    previousRollbackMut.isPending,
    rollbackMut.isPending,
    rollbackMut.variables,
  );

  return {
    updateBanner,
    updateStreamArmed,
    setUpdateStreamArmed,
    manualCheckPending,
    pendingUpdateConfirm,
    triggering,
    confirming: triggerUpdateMut.isPending,
    rollbackPendingId,
    runUpdateCheck,
    requestUpdateConfirm,
    triggerConfirmedUpdate,
    rollbackPrevious,
    rollbackRelease,
    clearBanner,
    closeUpdateConfirm,
  };
}
