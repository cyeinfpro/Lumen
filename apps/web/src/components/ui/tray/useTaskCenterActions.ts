"use client";

import { useCallback, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { userTaskQueryKeys } from "@/components/QueryProvider";
import {
  cancelTask,
  retryTask,
  type TaskItemResponse,
} from "@/lib/apiClient";
import { logWarn } from "@/lib/logger";
import { toast } from "@/components/ui/primitives";
import { taskKindPath } from "./taskCenterModel";

type TaskActionState = {
  busy: boolean;
  error: string | null;
};

export function useTaskCenterActions(userId: string | null | undefined) {
  const queryClient = useQueryClient();
  const locksRef = useRef(new Set<string>());
  const [states, setStates] = useState<Record<string, TaskActionState>>({});
  const run = useCallback(
    async (task: TaskItemResponse, action: "retry" | "cancel") => {
      if (locksRef.current.has(task.id)) return;
      locksRef.current.add(task.id);
      setStates((current) => ({
        ...current,
        [task.id]: { busy: true, error: null },
      }));
      try {
        if (action === "retry") {
          await retryTask(taskKindPath(task), task.id);
        } else {
          await cancelTask(taskKindPath(task), task.id);
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "任务操作失败";
        logWarn(`task-center.${action}_failed`, {
          scope: "tray",
          extra: { taskId: task.id, err: String(error) },
        });
        setStates((current) => ({
          ...current,
          [task.id]: { busy: true, error: message },
        }));
        toast.error(action === "retry" ? "重试任务失败" : "取消任务失败", {
          description: message,
        });
      } finally {
        await queryClient.invalidateQueries({
          queryKey: userTaskQueryKeys.all(userId),
        });
        locksRef.current.delete(task.id);
        setStates((current) => ({
          ...current,
          [task.id]: {
            busy: false,
            error: current[task.id]?.error ?? null,
          },
        }));
      }
    },
    [queryClient, userId],
  );

  return {
    busy: (taskId: string) => states[taskId]?.busy ?? false,
    error: (taskId: string) => states[taskId]?.error ?? null,
    retry: (task: TaskItemResponse) => void run(task, "retry"),
    cancel: (task: TaskItemResponse) => void run(task, "cancel"),
  };
}
