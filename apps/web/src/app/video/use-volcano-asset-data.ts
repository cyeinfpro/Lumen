"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";

import {
  getVideoAssetCapabilities,
  getVideoAssetUsage,
  listVideoAssetGroups,
  listVideoAssets,
} from "@/lib/apiClient";
import type {
  VideoAssetCapabilitiesOut,
  VideoAssetGroupOut,
  VideoAssetOut,
} from "@/lib/types";

import {
  settleVolcanoQuotaReservations,
  volcanoAssetErrorMessage,
  volcanoAssetStatusKind,
} from "./volcano-asset-domain";
import {
  assetListRequest,
  assetViewMatches,
} from "./volcano-asset-manager-state";
import type {
  ActiveSession,
  AssetStatusFilter,
  AssetTypeFilter,
  AssetViewSnapshot,
  UploadItem,
} from "./volcano-asset-manager-types";
import {
  ASSET_PAGE_SIZE,
  GROUP_PAGE_SIZE,
} from "./volcano-asset-manager-types";
import type { VolcanoUploadQueueController } from "./use-volcano-upload-queue";
import { isAbortError } from "./video-request-lifecycle";

function requestIsCurrent(
  controller: AbortController,
  controllerRef: RefObject<AbortController | null>,
  sessionActive: boolean,
): boolean {
  return (
    !controller.signal.aborted &&
    controllerRef.current === controller &&
    sessionActive
  );
}

function requestErrorIsCurrent(
  error: unknown,
  controller: AbortController,
  controllerRef: RefObject<AbortController | null>,
  sessionActive: boolean,
): boolean {
  return (
    !isAbortError(error) &&
    requestIsCurrent(controller, controllerRef, sessionActive)
  );
}

function finishRequest(
  controller: AbortController,
  controllerRef: RefObject<AbortController | null>,
  sessionActive: boolean,
  stopLoading: () => void,
): void {
  if (controllerRef.current !== controller) return;
  controllerRef.current = null;
  if (sessionActive) stopLoading();
}

function preferredGroupAfterRefresh(
  current: string | null,
  preferred: string | undefined,
  groups: VideoAssetGroupOut[],
): string | null {
  const desired = preferred ?? current;
  if (desired && groups.some((group) => group.id === desired)) {
    return desired;
  }
  return groups[0]?.id ?? null;
}

function syncUploadWithAssets(
  item: UploadItem,
  assetsById: ReadonlyMap<string, VideoAssetOut>,
): UploadItem {
  if (!item.assetId) return item;
  const asset = assetsById.get(item.assetId);
  if (!asset) return item;
  const kind = volcanoAssetStatusKind(asset.status);
  if (kind === "active") {
    return { ...item, file: null, phase: "ready" };
  }
  if (kind === "failed") {
    return {
      ...item,
      file: null,
      phase: "failed",
      error: asset.error_message || "火山处理失败",
    };
  }
  return item;
}

export type VolcanoAssetDataController = {
  capability: VideoAssetCapabilitiesOut | null;
  capabilityLoading: boolean;
  capabilityError: string | null;
  groups: VideoAssetGroupOut[];
  groupTotal: number | null;
  groupsLoading: boolean;
  groupsError: string | null;
  selectedGroupId: string | null;
  assets: VideoAssetOut[];
  assetsGroupId: string | null;
  assetTotal: number;
  projectAssetTotal: number | null;
  projectGroupTotal: number | null;
  quotaLoading: boolean;
  quotaError: string | null;
  assetPage: number;
  assetsLoading: boolean;
  assetsError: string | null;
  assetSearchInput: string;
  assetSearch: string;
  typeFilter: AssetTypeFilter;
  statusFilter: AssetStatusFilter;
  setGroups: Dispatch<SetStateAction<VideoAssetGroupOut[]>>;
  setGroupTotal: Dispatch<SetStateAction<number | null>>;
  setSelectedGroupId: Dispatch<SetStateAction<string | null>>;
  setAssets: Dispatch<SetStateAction<VideoAssetOut[]>>;
  setAssetSearchInput: Dispatch<SetStateAction<string>>;
  setAssetSearch: Dispatch<SetStateAction<string>>;
  setAssetPage: Dispatch<SetStateAction<number>>;
  refreshGroups: (
    preferredGroupId?: string,
    silent?: boolean,
    requestedSessionId?: number,
  ) => Promise<void>;
  refreshProjectAssetTotal: (
    silent?: boolean,
    requestedSessionId?: number,
  ) => Promise<void>;
  refreshAssets: (
    silent?: boolean,
    requestedSessionId?: number,
  ) => Promise<void>;
  loadCapability: (requestedSessionId?: number) => Promise<void>;
  abortDataRequests: () => void;
  resetData: () => void;
  selectGroup: (groupId: string) => void;
  changeTypeFilter: (value: AssetTypeFilter) => void;
  changeStatusFilter: (value: AssetStatusFilter) => void;
};

export function useVolcanoAssetData({
  model,
  activeSessionRef,
  isSessionActive,
  uploadQueue,
}: {
  model: string;
  activeSessionRef: RefObject<ActiveSession>;
  isSessionActive: (sessionId: number, expectedModel?: string) => boolean;
  uploadQueue: Pick<VolcanoUploadQueueController, "commitUploadQueue">;
}): VolcanoAssetDataController {
  const { commitUploadQueue } = uploadQueue;
  const capabilityAbortRef = useRef<AbortController | null>(null);
  const groupsAbortRef = useRef<AbortController | null>(null);
  const assetsAbortRef = useRef<AbortController | null>(null);
  const quotaAbortRef = useRef<AbortController | null>(null);
  const [capability, setCapability] =
    useState<VideoAssetCapabilitiesOut | null>(null);
  const [capabilityLoading, setCapabilityLoading] = useState(false);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [groups, setGroups] = useState<VideoAssetGroupOut[]>([]);
  const [groupTotal, setGroupTotal] = useState<number | null>(null);
  const [groupsLoading, setGroupsLoading] = useState(false);
  const [groupsError, setGroupsError] = useState<string | null>(null);
  const [selectedGroupId, setSelectedGroupId] = useState<string | null>(null);
  const [assets, setAssets] = useState<VideoAssetOut[]>([]);
  const [assetsGroupId, setAssetsGroupId] = useState<string | null>(null);
  const [assetTotal, setAssetTotal] = useState(0);
  const [projectAssetTotal, setProjectAssetTotal] = useState<number | null>(
    null,
  );
  const [projectGroupTotal, setProjectGroupTotal] = useState<number | null>(
    null,
  );
  const [quotaLoading, setQuotaLoading] = useState(false);
  const [quotaError, setQuotaError] = useState<string | null>(null);
  const [assetPage, setAssetPage] = useState(1);
  const [assetsLoading, setAssetsLoading] = useState(false);
  const [assetsError, setAssetsError] = useState<string | null>(null);
  const [assetSearchInput, setAssetSearchInput] = useState("");
  const [assetSearch, setAssetSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<AssetTypeFilter>("all");
  const [statusFilter, setStatusFilter] =
    useState<AssetStatusFilter>("all");
  const assetViewRef = useRef<AssetViewSnapshot>({
    capabilityReady: false,
    groupId: null,
    search: "",
    status: "all",
    type: "all",
    page: 1,
  });

  useEffect(() => {
    assetViewRef.current = {
      capabilityReady: Boolean(capability?.ready),
      groupId: selectedGroupId,
      search: assetSearch,
      status: statusFilter,
      type: typeFilter,
      page: assetPage,
    };
  }, [
    assetPage,
    assetSearch,
    capability?.ready,
    selectedGroupId,
    statusFilter,
    typeFilter,
  ]);

  const refreshGroups = useCallback(
    async (
      preferredGroupId?: string,
      silent = false,
      requestedSessionId = activeSessionRef.current.id,
    ) => {
      const requestedModel = model;
      if (!isSessionActive(requestedSessionId, requestedModel)) return;
      groupsAbortRef.current?.abort();
      const controller = new AbortController();
      groupsAbortRef.current = controller;
      if (!silent) setGroupsLoading(true);
      setGroupsError(null);
      try {
        const result = await listVideoAssetGroups({
          model,
          page_number: 1,
          page_size: GROUP_PAGE_SIZE,
          signal: controller.signal,
        });
        if (
          !requestIsCurrent(
            controller,
            groupsAbortRef,
            isSessionActive(requestedSessionId, requestedModel),
          )
        ) {
          return;
        }
        setGroups(result.items);
        setGroupTotal(Math.max(result.total_count, result.items.length));
        setSelectedGroupId((current) =>
          preferredGroupAfterRefresh(
            current,
            preferredGroupId,
            result.items,
          ),
        );
      } catch (error) {
        if (
          requestErrorIsCurrent(
            error,
            controller,
            groupsAbortRef,
            isSessionActive(requestedSessionId, requestedModel),
          )
        ) {
          if (!silent) setGroupTotal(null);
          setGroupsError(volcanoAssetErrorMessage(error, "素材组加载失败"));
        }
      } finally {
        finishRequest(
          controller,
          groupsAbortRef,
          isSessionActive(requestedSessionId, requestedModel),
          () => setGroupsLoading(false),
        );
      }
    },
    [activeSessionRef, isSessionActive, model],
  );

  const refreshProjectAssetTotal = useCallback(
    async (
      silent = false,
      requestedSessionId = activeSessionRef.current.id,
    ) => {
      const requestedModel = model;
      if (!isSessionActive(requestedSessionId, requestedModel)) return;
      quotaAbortRef.current?.abort();
      const controller = new AbortController();
      quotaAbortRef.current = controller;
      if (!silent) setQuotaLoading(true);
      setQuotaError(null);
      try {
        const result = await getVideoAssetUsage(requestedModel, {
          signal: controller.signal,
        });
        if (
          !requestIsCurrent(
            controller,
            quotaAbortRef,
            isSessionActive(requestedSessionId, requestedModel),
          )
        ) {
          return;
        }
        const remoteAssetTotal = Math.max(0, result.assets_used);
        const remoteGroupTotal = Math.max(0, result.asset_groups_used);
        setProjectAssetTotal(remoteAssetTotal);
        setProjectGroupTotal(remoteGroupTotal);
        commitUploadQueue(requestedModel, (current) =>
          settleVolcanoQuotaReservations(current, remoteAssetTotal),
        );
      } catch (error) {
        if (
          requestErrorIsCurrent(
            error,
            controller,
            quotaAbortRef,
            isSessionActive(requestedSessionId, requestedModel),
          )
        ) {
          if (!silent) {
            setProjectAssetTotal(null);
            setProjectGroupTotal(null);
          }
          setQuotaError(volcanoAssetErrorMessage(error, "Project 配额读取失败"));
        }
      } finally {
        finishRequest(
          controller,
          quotaAbortRef,
          isSessionActive(requestedSessionId, requestedModel),
          () => setQuotaLoading(false),
        );
      }
    },
    [
      activeSessionRef,
      isSessionActive,
      model,
      commitUploadQueue,
    ],
  );

  const refreshAssets = useCallback(
    async (
      silent = false,
      requestedSessionId = activeSessionRef.current.id,
    ) => {
      const requestedModel = model;
      const requestedView = { ...assetViewRef.current };
      const requestedGroupId = requestedView.groupId;
      if (!isSessionActive(requestedSessionId, requestedModel)) return;
      if (!requestedView.capabilityReady || !requestedGroupId) {
        setAssets([]);
        setAssetsGroupId(null);
        setAssetTotal(0);
        return;
      }
      assetsAbortRef.current?.abort();
      const controller = new AbortController();
      assetsAbortRef.current = controller;
      if (!silent) setAssetsLoading(true);
      setAssetsError(null);
      try {
        const result = await listVideoAssets({
          model: requestedModel,
          group_ids: [requestedGroupId],
          ...assetListRequest(requestedView, ASSET_PAGE_SIZE),
          signal: controller.signal,
        });
        if (
          !requestIsCurrent(
            controller,
            assetsAbortRef,
            isSessionActive(requestedSessionId, requestedModel),
          ) ||
          activeSessionRef.current.model !== requestedModel ||
          !assetViewMatches(assetViewRef.current, requestedView)
        ) {
          return;
        }
        setAssets(result.items);
        setAssetsGroupId(requestedGroupId);
        setAssetTotal(result.total_count);
        const byId = new Map(result.items.map((item) => [item.id, item]));
        commitUploadQueue(requestedModel, (current) =>
          current.map((item) => syncUploadWithAssets(item, byId)),
        );
      } catch (error) {
        if (
          requestErrorIsCurrent(
            error,
            controller,
            assetsAbortRef,
            isSessionActive(requestedSessionId, requestedModel),
          ) &&
          assetViewMatches(assetViewRef.current, requestedView)
        ) {
          setAssetsError(volcanoAssetErrorMessage(error, "素材加载失败"));
        }
      } finally {
        finishRequest(
          controller,
          assetsAbortRef,
          isSessionActive(requestedSessionId, requestedModel),
          () => setAssetsLoading(false),
        );
      }
    },
    [
      activeSessionRef,
      isSessionActive,
      model,
      commitUploadQueue,
    ],
  );

  const loadCapability = useCallback(
    async (requestedSessionId = activeSessionRef.current.id) => {
      const requestedModel = model;
      if (!isSessionActive(requestedSessionId, requestedModel)) return;
      capabilityAbortRef.current?.abort();
      const controller = new AbortController();
      capabilityAbortRef.current = controller;
      setCapabilityLoading(true);
      setCapabilityError(null);
      setCapability(null);
      try {
        const result = await getVideoAssetCapabilities(requestedModel, {
          signal: controller.signal,
        });
        if (
          controller.signal.aborted ||
          capabilityAbortRef.current !== controller ||
          !isSessionActive(requestedSessionId, requestedModel)
        ) {
          return;
        }
        setCapability(result);
        if (result.ready) {
          await Promise.all([
            refreshGroups(undefined, false, requestedSessionId),
            refreshProjectAssetTotal(false, requestedSessionId),
          ]);
        } else {
          setGroups([]);
          setGroupTotal(null);
          setProjectAssetTotal(null);
          setProjectGroupTotal(null);
          setSelectedGroupId(null);
        }
      } catch (error) {
        if (
          !isAbortError(error) &&
          capabilityAbortRef.current === controller &&
          isSessionActive(requestedSessionId, requestedModel)
        ) {
          setCapabilityError(volcanoAssetErrorMessage(error, "能力检查失败"));
        }
      } finally {
        if (capabilityAbortRef.current === controller) {
          capabilityAbortRef.current = null;
          if (isSessionActive(requestedSessionId, requestedModel)) {
            setCapabilityLoading(false);
          }
        }
      }
    },
    [
      activeSessionRef,
      isSessionActive,
      model,
      refreshGroups,
      refreshProjectAssetTotal,
    ],
  );

  const abortDataRequests = useCallback(() => {
    capabilityAbortRef.current?.abort();
    groupsAbortRef.current?.abort();
    assetsAbortRef.current?.abort();
    quotaAbortRef.current?.abort();
    capabilityAbortRef.current = null;
    groupsAbortRef.current = null;
    assetsAbortRef.current = null;
    quotaAbortRef.current = null;
  }, []);

  const resetData = useCallback(() => {
    setCapability(null);
    setCapabilityLoading(false);
    setCapabilityError(null);
    setGroups([]);
    setGroupsLoading(false);
    setGroupsError(null);
    setGroupTotal(null);
    setSelectedGroupId(null);
    setAssets([]);
    setAssetsGroupId(null);
    setAssetTotal(0);
    setAssetsLoading(false);
    setAssetsError(null);
    setProjectAssetTotal(null);
    setProjectGroupTotal(null);
    setQuotaLoading(false);
    setQuotaError(null);
    setAssetSearchInput("");
    setAssetSearch("");
    setAssetPage(1);
    setTypeFilter("all");
    setStatusFilter("all");
  }, []);

  const selectGroup = useCallback((groupId: string) => {
    assetsAbortRef.current?.abort();
    setAssetPage(1);
    setAssets([]);
    setAssetsGroupId(null);
    setAssetTotal(0);
    setSelectedGroupId(groupId);
  }, []);

  const changeTypeFilter = useCallback((value: AssetTypeFilter) => {
    assetsAbortRef.current?.abort();
    setAssetPage(1);
    setTypeFilter(value);
  }, []);

  const changeStatusFilter = useCallback((value: AssetStatusFilter) => {
    assetsAbortRef.current?.abort();
    setAssetPage(1);
    setStatusFilter(value);
  }, []);

  return {
    capability,
    capabilityLoading,
    capabilityError,
    groups,
    groupTotal,
    groupsLoading,
    groupsError,
    selectedGroupId,
    assets,
    assetsGroupId,
    assetTotal,
    projectAssetTotal,
    projectGroupTotal,
    quotaLoading,
    quotaError,
    assetPage,
    assetsLoading,
    assetsError,
    assetSearchInput,
    assetSearch,
    typeFilter,
    statusFilter,
    setGroups,
    setGroupTotal,
    setSelectedGroupId,
    setAssets,
    setAssetSearchInput,
    setAssetSearch,
    setAssetPage,
    refreshGroups,
    refreshProjectAssetTotal,
    refreshAssets,
    loadCapability,
    abortDataRequests,
    resetData,
    selectGroup,
    changeTypeFilter,
    changeStatusFilter,
  };
}
