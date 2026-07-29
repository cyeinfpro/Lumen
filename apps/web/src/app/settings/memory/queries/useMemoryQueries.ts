"use client";

import { useQuery } from "@tanstack/react-query";

import { userMemoryQueryKeys } from "@/components/QueryProvider";
import {
  getMemorySettings,
  listMemories,
  listMemoryScopes,
  listMemoryStaging,
  listMemoryTimeline,
} from "@/lib/apiClient";

import type { MemoryPageQueryScope } from "../types";

export function useMemoryQueries(
  userScope: MemoryPageQueryScope,
  selectedScope: string,
) {
  const settingsQuery = useQuery({
    queryKey: userMemoryQueryKeys.settings(userScope.userId),
    queryFn: getMemorySettings,
    enabled: userScope.enabled,
  });
  const scopesQuery = useQuery({
    queryKey: userMemoryQueryKeys.scopes(userScope.userId),
    queryFn: listMemoryScopes,
    enabled: userScope.enabled,
  });
  const memoriesQuery = useQuery({
    queryKey: userMemoryQueryKeys.items(userScope.userId, selectedScope),
    queryFn: () =>
      listMemories(selectedScope === "all" ? {} : { scope_id: selectedScope }),
    enabled: userScope.enabled,
  });
  const stagingQuery = useQuery({
    queryKey: userMemoryQueryKeys.staging(userScope.userId),
    queryFn: listMemoryStaging,
    enabled: userScope.enabled,
  });
  const timelineQuery = useQuery({
    queryKey: userMemoryQueryKeys.timeline(userScope.userId),
    queryFn: () => listMemoryTimeline(),
    enabled: userScope.enabled,
  });

  return {
    settingsQuery,
    scopesQuery,
    memoriesQuery,
    stagingQuery,
    timelineQuery,
  };
}
