"use client";

import { useEffect, useRef } from "react";

import {
  useAdminProxiesQuery,
  useAdminCheckUpdateQuery,
  useAdminReleasesQuery,
  useAdminUpdateStatusQuery,
  useAdminUpdateVersionQuery,
  useSystemSettingsQuery,
  useUpdateSystemSettingsMutation,
} from "@/lib/queries";
import type { AdminUpdateStatusOut } from "@/lib/apiClient";
import {
  anyPending,
  updatePollInterval,
  updateRunningFor,
} from "./AdminUpdatePanel.helpers";
import {
  useAdminUpdateStream,
  useDisarmUpdateStream,
} from "./AdminUpdatePanel.hooks";

export function useAdminUpdatePanelQueries() {
  const streamConnectedRef = useRef(false);
  const settingsQ = useSystemSettingsQuery({ retry: false });
  const proxiesQ = useAdminProxiesQuery({ retry: false });
  const updateSettingsMut = useUpdateSystemSettingsMutation();
  const updateStatusQ = useAdminUpdateStatusQuery({
    retry: false,
    refetchInterval: (query) =>
      updatePollInterval(streamConnectedRef.current, query.state.data?.running),
  });
  const updateVersionQ = useAdminUpdateVersionQuery({ retry: false });
  const updateCheckQ = useAdminCheckUpdateQuery(false, { retry: false });
  const releasesQ = useAdminReleasesQuery({ retry: false });

  return {
    streamConnectedRef,
    settingsQ,
    proxiesQ,
    updateSettingsMut,
    updateStatusQ,
    updateVersionQ,
    updateCheckQ,
    releasesQ,
  };
}

interface UseAdminUpdateRuntimeOptions {
  status: AdminUpdateStatusOut | undefined;
  streamConnectedRef: { current: boolean };
  updateStreamArmed: boolean;
  setUpdateStreamArmed: (armed: boolean) => void;
  triggering: boolean;
}

export function useAdminUpdateRuntime({
  status,
  streamConnectedRef,
  updateStreamArmed,
  setUpdateStreamArmed,
  triggering,
}: UseAdminUpdateRuntimeOptions) {
  const updateRunning = updateRunningFor(status);
  const sseEnabled = anyPending(updateRunning, updateStreamArmed, triggering);
  const stream = useAdminUpdateStream(sseEnabled);

  useEffect(() => {
    streamConnectedRef.current = stream.streamStatus === "open";
  }, [stream.streamStatus, streamConnectedRef]);

  useDisarmUpdateStream(
    updateStreamArmed,
    setUpdateStreamArmed,
    triggering,
    updateRunning,
  );

  return stream;
}
