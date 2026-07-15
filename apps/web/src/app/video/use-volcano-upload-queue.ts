"use client";

import {
  useCallback,
  useRef,
  useState,
  type RefObject,
} from "react";

import { pauseUploadQueue } from "./volcano-asset-manager-state";
import type {
  ActiveSession,
  UploadItem,
} from "./volcano-asset-manager-types";

export type VolcanoUploadQueueController = {
  uploads: UploadItem[];
  uploadsRef: RefObject<UploadItem[]>;
  uploadQueuesRef: RefObject<Map<string, UploadItem[]>>;
  uploadNamesRef: RefObject<Map<string, string>>;
  uploadControllersRef: RefObject<Map<string, AbortController>>;
  pollAbortRef: RefObject<AbortController | null>;
  createAssetQueueRef: RefObject<Promise<void>>;
  nextCreateAssetAtRef: RefObject<number>;
  commitUploadQueue: (
    queueModel: string,
    updater: (current: UploadItem[]) => UploadItem[],
  ) => UploadItem[];
  updateUpload: (
    id: string,
    patch: Partial<UploadItem>,
    sessionId?: number,
    queueModel?: string,
  ) => void;
  abortUploadRequests: () => void;
  resetUploadScheduling: () => void;
  restoreUploadQueue: (queueModel: string) => UploadItem[];
  pauseActiveUploadQueue: (queueModel: string) => UploadItem[];
  showUploads: (items: UploadItem[]) => void;
};

export function useVolcanoUploadQueue(
  activeSessionRef: RefObject<ActiveSession>,
): VolcanoUploadQueueController {
  const uploadControllersRef = useRef(new Map<string, AbortController>());
  const pollAbortRef = useRef<AbortController | null>(null);
  const uploadsRef = useRef<UploadItem[]>([]);
  const uploadQueuesRef = useRef(new Map<string, UploadItem[]>());
  const uploadNamesRef = useRef(new Map<string, string>());
  const createAssetQueueRef = useRef<Promise<void>>(Promise.resolve());
  const nextCreateAssetAtRef = useRef(0);
  const [uploads, setUploads] = useState<UploadItem[]>([]);

  const commitUploadQueue = useCallback(
    (
      queueModel: string,
      updater: (current: UploadItem[]) => UploadItem[],
    ): UploadItem[] => {
      const current =
        uploadQueuesRef.current.get(queueModel) ??
        (activeSessionRef.current.model === queueModel
          ? uploadsRef.current
          : []);
      const next = updater(current);
      uploadQueuesRef.current.set(queueModel, next);
      const active = activeSessionRef.current;
      if (active.model === queueModel) {
        uploadsRef.current = next;
        if (active.open) setUploads(next);
      }
      return next;
    },
    [activeSessionRef],
  );

  const updateUpload = useCallback(
    (
      id: string,
      patch: Partial<UploadItem>,
      _sessionId?: number,
      queueModel?: string,
    ): void => {
      let resolvedModel = queueModel;
      if (!resolvedModel) {
        for (const [candidateModel, queue] of uploadQueuesRef.current) {
          if (queue.some((item) => item.id === id)) {
            resolvedModel = candidateModel;
            break;
          }
        }
      }
      resolvedModel ??= activeSessionRef.current.model;
      commitUploadQueue(resolvedModel, (current) => {
        let changed = false;
        const next = current.map((item) => {
          if (item.id !== id) return item;
          const patchChanged = Object.entries(patch).some(
            ([key, value]) => item[key as keyof UploadItem] !== value,
          );
          if (!patchChanged) return item;
          changed = true;
          return { ...item, ...patch };
        });
        return changed ? next : current;
      });
    },
    [activeSessionRef, commitUploadQueue],
  );

  const abortUploadRequests = useCallback(() => {
    pollAbortRef.current?.abort();
    pollAbortRef.current = null;
    for (const controller of uploadControllersRef.current.values()) {
      controller.abort();
    }
    uploadControllersRef.current.clear();
  }, []);

  const resetUploadScheduling = useCallback(() => {
    createAssetQueueRef.current = Promise.resolve();
    nextCreateAssetAtRef.current = 0;
  }, []);

  const restoreUploadQueue = useCallback((queueModel: string) => {
    const restored = pauseUploadQueue(
      uploadQueuesRef.current.get(queueModel) ?? [],
    );
    uploadQueuesRef.current.set(queueModel, restored);
    uploadsRef.current = restored;
    uploadNamesRef.current.clear();
    for (const item of restored) {
      uploadNamesRef.current.set(item.id, item.name);
    }
    return restored;
  }, []);

  const pauseActiveUploadQueue = useCallback((queueModel: string) => {
    const paused = pauseUploadQueue(uploadsRef.current);
    uploadQueuesRef.current.set(queueModel, paused);
    uploadsRef.current = paused;
    return paused;
  }, []);

  const showUploads = useCallback((items: UploadItem[]) => {
    uploadsRef.current = items;
    setUploads(items);
  }, []);

  return {
    uploads,
    uploadsRef,
    uploadQueuesRef,
    uploadNamesRef,
    uploadControllersRef,
    pollAbortRef,
    createAssetQueueRef,
    nextCreateAssetAtRef,
    commitUploadQueue,
    updateUpload,
    abortUploadRequests,
    resetUploadScheduling,
    restoreUploadQueue,
    pauseActiveUploadQueue,
    showUploads,
  };
}
