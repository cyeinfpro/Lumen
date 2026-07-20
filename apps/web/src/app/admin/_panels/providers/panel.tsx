"use client";

import { AnimatePresence, motion } from "framer-motion";
import {
  Activity,
  AlertCircle,
  Check,
  CloudOff,
  Pencil,
  Plus,
  Server,
  X,
} from "lucide-react";
import type { ProviderProxyOut } from "@/lib/types";
import { Button, IconButton } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import { ErrorBlock } from "../../_components/AdminFeedback";
import { ProviderEditActions } from "../../_components/ProviderEditActions";
import {
  AutoProbeSettings,
  DraftList,
  PriorityGroupView,
  RequestStatsPanel,
  StatsRow,
  WeightBar,
} from "./views";
import type { ProviderPanelState } from "./useProviderPanelState";

export function ProviderPanelView({
  state,
  proxies,
}: {
  state: ProviderPanelState;
  proxies: ProviderProxyOut[];
}) {
  return (
    <section className="space-y-5 pb-28">
      <ProviderPanelHeader state={state} />
      <ProviderPanelMessages state={state} />
      <ProviderOperationalPanels state={state} />
      <ProviderPanelContent state={state} proxies={proxies} />
      <ProviderEditActions
        open={state.isEditing}
        draftCount={state.drafts?.length ?? 0}
        saving={state.updateSaving}
        onAdd={state.addProvider}
        onCancel={state.cancelEdit}
        onSave={state.validateAndSave}
      />
    </section>
  );
}

function ProviderPanelHeader({
  state,
}: {
  state: ProviderPanelState;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-panel)] border border-[var(--color-lumen-amber)]/25 bg-[var(--color-lumen-amber)]/15">
              <Server className="h-4 w-4 text-[var(--color-lumen-amber)]" />
            </div>
            <div>
              <h3 className="text-sm font-medium text-[var(--fg-0)]">
                供应商池
              </h3>
              <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                加权轮询 · 断路器 · 主动探活
              </p>
            </div>
          </div>
        </div>
        {!state.isEditing && (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={state.onProbeAll}
              disabled={state.probing || state.serverItems.length === 0}
              loading={state.probing}
              leftIcon={
                !state.probing ? <Activity className="h-3 w-3" /> : undefined
              }
            >
              {state.probing ? "探活中" : "手动探活"}
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={state.startEdit}
              leftIcon={<Pencil className="h-3 w-3" />}
            >
              {copy.action.edit}
            </Button>
          </div>
        )}
      </div>
      {state.serverItems.length > 0 && !state.isEditing && (
        <StatsRow
          total={state.serverItems.length}
          enabled={state.enabledCount}
          healthy={state.healthyCount}
          probing={state.probing}
          probedAt={state.probeTimestamp}
          source={state.source}
        />
      )}
    </div>
  );
}

function ProviderPanelMessages({
  state,
}: {
  state: ProviderPanelState;
}) {
  return (
    <AnimatePresence>
      {state.globalError && (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-4 py-3 type-body-sm text-danger"
        >
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="flex-1">{state.globalError}</span>
          <IconButton
            variant="ghost"
            size="sm"
            onClick={state.clearGlobalError}
            aria-label={copy.action.close}
            className="shrink-0"
          >
            <X className="h-3.5 w-3.5" />
          </IconButton>
        </motion.div>
      )}
      {state.savedAt && (
        <motion.div
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          className="flex items-center gap-2 rounded-[var(--radius-card)] border border-success-border bg-success-soft px-4 py-3 type-body-sm text-success"
        >
          <Check className="h-4 w-4" /> {copy.state.saved}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function ProviderOperationalPanels({
  state,
}: {
  state: ProviderPanelState;
}) {
  if (state.serverItems.length === 0 || state.isEditing) return null;
  return (
    <>
      <AutoProbeSettings
        interval={state.autoProbeInterval}
        onChangeInterval={state.onToggleAutoProbe}
        saving={state.settingsSaving}
      />
      {state.statsItems && (
        <RequestStatsPanel items={state.statsItems} />
      )}
      {state.serverItems.length >= 2 && (
        <WeightBar items={state.serverItems} />
      )}
    </>
  );
}

function ProviderPanelContent({
  state,
  proxies,
}: {
  state: ProviderPanelState;
  proxies: ProviderProxyOut[];
}) {
  if (state.providersQuery.isLoading) return <ProviderPanelSkeleton />;
  if (state.providersQuery.isError) {
    return (
      <ErrorBlock
        message={state.providersQuery.error?.message ?? "未知错误"}
        onRetry={() => void state.providersQuery.refetch()}
      />
    );
  }
  if (state.isEditing && state.drafts) {
    return (
      <div className="space-y-5">
        <DraftList
          drafts={state.drafts}
          proxies={proxies}
          editingIdx={state.editingIdx}
          deleteConfirmIdx={state.deleteConfirmIdx}
          fieldErrors={state.draftErrors}
          serverKeyHints={state.serverKeyHints}
          newCardRef={state.newCardRef}
          onEdit={state.setEditingIdx}
          onUpdate={state.updateDraft}
          onRemove={state.removeProvider}
          onMove={state.moveProvider}
          onDeleteConfirm={state.setDeleteConfirmIdx}
        />
      </div>
    );
  }
  if (state.serverItems.length === 0) {
    return (
      <EmptyProvidersState
        startEdit={state.startEdit}
        addProvider={state.addProvider}
      />
    );
  }
  return <ProviderGroups state={state} />;
}

function ProviderPanelSkeleton() {
  return (
    <div className="space-y-3">
      {[0, 1, 2].map((index) => (
        <div
          key={`skel-${index}`}
          className="h-28 animate-pulse rounded-[var(--radius-dialog)] bg-white/5"
          style={{ animationDelay: `${index * 80}ms` }}
        />
      ))}
    </div>
  );
}

function EmptyProvidersState({
  startEdit,
  addProvider,
}: {
  startEdit: () => void;
  addProvider: () => void;
}) {
  return (
    <div className="rounded-[var(--radius-dialog)] border border-[var(--border)] bg-[var(--bg-1)]/60 py-16 text-center backdrop-blur-sm">
      <div className="flex flex-col items-center gap-4">
        <div className="flex h-14 w-14 items-center justify-center rounded-[var(--radius-dialog)] border border-[var(--border)] bg-white/5">
          <CloudOff className="h-6 w-6 text-[var(--fg-2)]" />
        </div>
        <div>
          <p className="type-body-sm text-[var(--fg-1)]">还没有供应商</p>
          <p className="mt-1 type-caption text-[var(--fg-2)]">
            添加至少一个供应商后，请求会从池里选择可用账号。
          </p>
        </div>
        <Button
          variant="primary"
          size="md"
          onClick={() => {
            startEdit();
            setTimeout(addProvider, 50);
          }}
          leftIcon={<Plus className="h-3.5 w-3.5" />}
        >
          添加首个供应商
        </Button>
      </div>
    </div>
  );
}

function ProviderGroups({
  state,
}: {
  state: ProviderPanelState;
}) {
  return (
    <div className="space-y-5">
      {state.groups.map((group) => (
        <PriorityGroupView
          key={group.priority}
          group={group}
          probeMap={state.probeMap}
          statsMap={state.statsMap}
          probing={state.probing}
          totalGroups={state.groups.length}
          onProbeSingle={state.onProbeSingle}
          onToggleEnabled={state.toggleProviderEnabled}
          onSavePurposes={state.quickSavePurposes}
          quickSaving={state.quickSaving}
        />
      ))}
    </div>
  );
}
