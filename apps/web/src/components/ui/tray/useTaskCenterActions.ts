"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { userTaskQueryKeys } from "@/components/QueryProvider";
import {
  cancelTask,
  retryTask,
  type TaskItemResponse,
} from "@/lib/apiClient";
import { logWarn } from "@/lib/logger";
import { taskKindPath } from "./taskCenterModel";

export function useTaskCenterActions(userId: string | null | undefined) {
  const queryClient = useQueryClient();
  const retryMutation = useMutation({
    mutationFn: async (task: TaskItemResponse) => {
      await retryTask(taskKindPath(task), task.id);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: userTaskQueryKeys.all(userId),
      });
    },
    onError: (error, task) => {
      logWarn("task-center.retry_failed", {
        scope: "tray",
        extra: { taskId: task.id, err: String(error) },
      });
    },
  });
  const cancelMutation = useMutation({
    mutationFn: async (task: TaskItemResponse) => {
      await cancelTask(taskKindPath(task), task.id);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: userTaskQueryKeys.all(userId),
      });
    },
    onError: (error, task) => {
      logWarn("task-center.cancel_failed", {
        scope: "tray",
        extra: { taskId: task.id, err: String(error) },
      });
    },
  });

  return {
    busy: retryMutation.isPending || cancelMutation.isPending,
    retry: retryMutation.mutate,
    cancel: cancelMutation.mutate,
  };
}
