"use client";

import { useQueryClient } from "@tanstack/react-query";

import { UpdateAvailableCard } from "@/components/admin/UpdateAvailableCard";
import { useAdminUpdateActions } from "./AdminUpdatePanel.actions";
import { LumenUpdateBlock } from "./AdminUpdatePanel.console";
import { UpdateConfirmDialog } from "./AdminUpdatePanel.dialogs";
import { UpdateNetworkSettingsCard } from "./AdminUpdatePanel.network";
import {
  useAdminUpdatePanelQueries,
  useAdminUpdateRuntime,
} from "./AdminUpdatePanel.state";

export function AdminUpdatePanel() {
  return <AdminUpdatePanelInner />;
}

function AdminUpdatePanelInner() {
  const queryClient = useQueryClient();
  const queries = useAdminUpdatePanelQueries();
  const actions = useAdminUpdateActions({
    queryClient,
    updateVersion: queries.updateVersionQ.data,
  });
  const runtime = useAdminUpdateRuntime({
    status: queries.updateStatusQ.data,
    streamConnectedRef: queries.streamConnectedRef,
    updateStreamArmed: actions.updateStreamArmed,
    setUpdateStreamArmed: actions.setUpdateStreamArmed,
    triggering: actions.triggering,
  });

  const requestUpdateConfirm = () => {
    actions.requestUpdateConfirm(queries.updateCheckQ.data);
  };
  const triggerConfirmedUpdate = () => {
    runtime.clearLogs();
    actions.triggerConfirmedUpdate();
  };
  const rollbackPrevious = () => {
    runtime.clearLogs();
    actions.rollbackPrevious();
  };
  const rollbackRelease = (releaseId: string) => {
    runtime.clearLogs();
    actions.rollbackRelease(releaseId);
  };

  return (
    <section className="space-y-3">
      <UpdateNetworkSettingsCard
        settings={queries.settingsQ.data?.items ?? []}
        proxies={queries.proxiesQ.data?.items ?? []}
        loading={queries.settingsQ.isLoading || queries.proxiesQ.isLoading}
        saving={queries.updateSettingsMut.isPending}
        error={queries.settingsQ.error ?? queries.proxiesQ.error ?? null}
        onRetry={() => {
          void queries.settingsQ.refetch();
          void queries.proxiesQ.refetch();
        }}
        onSave={(items) => queries.updateSettingsMut.mutate(items)}
      />

      <UpdateAvailableCard
        check={queries.updateCheckQ.data}
        status={queries.updateStatusQ.data}
        version={queries.updateVersionQ.data}
        checking={queries.updateCheckQ.isLoading || actions.manualCheckPending}
        triggering={actions.triggering}
        onCheck={(force) => {
          void actions.runUpdateCheck(force);
        }}
        onTrigger={requestUpdateConfirm}
        onRollbackPrevious={rollbackPrevious}
      />

      <LumenUpdateBlock
        status={queries.updateStatusQ.data}
        loading={queries.updateStatusQ.isLoading}
        error={queries.updateStatusQ.error}
        triggering={actions.triggering}
        banner={actions.updateBanner}
        releases={queries.releasesQ.data}
        releasesLoading={queries.releasesQ.isLoading}
        releasesError={queries.releasesQ.error}
        rollbackPendingId={actions.rollbackPendingId}
        logBuffer={runtime.logBuffer}
        streamStatus={runtime.streamStatus}
        onTrigger={requestUpdateConfirm}
        onRefresh={() => {
          void queries.updateStatusQ.refetch();
          void queries.releasesQ.refetch();
        }}
        onRollbackPrevious={rollbackPrevious}
        onRollback={rollbackRelease}
        onClearBanner={actions.clearBanner}
      />

      <UpdateConfirmDialog
        pending={actions.pendingUpdateConfirm}
        confirming={actions.confirming}
        onClose={actions.closeUpdateConfirm}
        onConfirm={triggerConfirmedUpdate}
      />
    </section>
  );
}
