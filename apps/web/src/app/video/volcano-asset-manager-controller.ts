"use client";

import { useCallback, useEffect, useRef } from "react";

import type { ActiveSession, Notice } from "./volcano-asset-manager-types";
import { useVolcanoAssetData } from "./use-volcano-asset-data";
import { useVolcanoOperationController } from "./use-volcano-operation-controller";
import { useVolcanoUploadQueue } from "./use-volcano-upload-queue";

export function useVolcanoAssetManagerController({
  open,
  model,
  resetSessionUi,
  setNotice,
}: {
  open: boolean;
  model: string;
  resetSessionUi: () => void;
  setNotice: (notice: Notice) => void;
}) {
  const sessionCounterRef = useRef(0);
  const activeSessionRef = useRef<ActiveSession>({
    id: 0,
    open: false,
    model,
  });

  const isSessionActive = useCallback(
    (sessionId: number, expectedModel?: string) => {
      const current = activeSessionRef.current;
      return (
        current.open &&
        current.id === sessionId &&
        (!expectedModel || current.model === expectedModel)
      );
    },
    [],
  );

  const uploadQueue = useVolcanoUploadQueue(activeSessionRef);
  const operationController = useVolcanoOperationController({
    activeSessionRef,
    isSessionActive,
    setNotice,
  });
  const assetData = useVolcanoAssetData({
    model,
    activeSessionRef,
    isSessionActive,
    uploadQueue,
  });
  const {
    abortUploadRequests,
    pauseActiveUploadQueue,
    resetUploadScheduling,
    restoreUploadQueue,
    showUploads,
  } = uploadQueue;
  const {
    abortOperationRequests,
    pauseActiveOperationQueue,
    restoreOperationQueue,
    resumePausedOperations,
    showOperations,
  } = operationController;
  const {
    abortDataRequests,
    assetSearchInput,
    loadCapability,
    resetData,
    setAssetPage,
    setAssetSearch,
  } = assetData;

  const abortSessionRequests = useCallback(() => {
    abortDataRequests();
    abortUploadRequests();
    abortOperationRequests();
  }, [abortDataRequests, abortOperationRequests, abortUploadRequests]);

  useEffect(() => {
    const sessionId = sessionCounterRef.current + 1;
    sessionCounterRef.current = sessionId;
    activeSessionRef.current = { id: sessionId, open, model };
    abortSessionRequests();
    resetUploadScheduling();
    const restoredUploads = restoreUploadQueue(model);
    const restoredOperations = restoreOperationQueue(model, sessionId);
    if (!open) return;
    const timer = window.setTimeout(() => {
      if (!isSessionActive(sessionId, model)) return;
      resetSessionUi();
      resetData();
      void loadCapability(sessionId);
      showUploads(restoredUploads);
      showOperations(restoredOperations);
      resumePausedOperations(restoredOperations);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      pauseActiveUploadQueue(model);
      pauseActiveOperationQueue(model);
      if (activeSessionRef.current.id === sessionId) {
        activeSessionRef.current = {
          id: sessionId,
          open: false,
          model,
        };
      }
      abortSessionRequests();
    };
  }, [
    abortSessionRequests,
    isSessionActive,
    loadCapability,
    model,
    open,
    pauseActiveOperationQueue,
    pauseActiveUploadQueue,
    resetData,
    resetSessionUi,
    resetUploadScheduling,
    restoreOperationQueue,
    restoreUploadQueue,
    resumePausedOperations,
    showOperations,
    showUploads,
  ]);

  useEffect(() => {
    if (!open) return;
    const sessionId = activeSessionRef.current.id;
    const timer = window.setTimeout(() => {
      if (!isSessionActive(sessionId, model)) return;
      setAssetSearch(assetSearchInput.trim());
      setAssetPage(1);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [
    assetSearchInput,
    isSessionActive,
    model,
    open,
    setAssetPage,
    setAssetSearch,
  ]);

  return {
    activeSessionRef,
    isSessionActive,
    uploadQueue,
    operationController,
    assetData,
  };
}
