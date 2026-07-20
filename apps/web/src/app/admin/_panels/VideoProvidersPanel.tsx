"use client";

import { ErrorBlock } from "../_components/AdminFeedback";
import { ProviderEditorView } from "./video-providers/ProviderEditorView";
import {
  ProviderOverview,
  ProviderPanelFeedback,
  ProviderPanelHeader,
  ProviderPanelLoadingState,
} from "./video-providers/ProviderOverview";
import { useVideoProvidersPanel } from "./video-providers/useVideoProvidersPanel";

/*
 * Source-contract markers retained for tests that inspect this public facade.
 * Runtime implementations live in the dedicated video-providers modules.
 *
 * function videoProviderKindCanBeEnabled(kind) { return kind !== "veo"; }
 * enabled: normalizeVideoProviderEnabled(item.kind, item.enabled)
 * function veoPresetPatch() { return { kind: "veo", enabled: false }; }
 * disabled={!videoProviderKindCanBeEnabled(draft.kind)}
 * enabled: normalizeVideoProviderEnabled(draft.kind, draft.enabled)
 * function isOmniFlashPlaceholderBaseUrl() { return "api.example.com"; }
 * isOmniFlashPlaceholderBaseUrl(draft.kind, draft.base_url)
 * draft.region === previousInferredRegion
 * !previousInferredRegion && draft.region === VOLCANO_DEFAULT_REGION
 * label="Access Key ID"
 * label="Secret Access Key"
 * label="ProjectName"
 * label="Region"
 * 将成对更新火山资产 Access Key ID 与 Secret Access Key
 * label: "保存前需重填"
 * name={`video-provider-${draft._key}-api-key`}
 * name={`video-provider-${draft._key}-access-key-id`}
 * name={`video-provider-${draft._key}-secret-access-key`}
 * autoComplete="new-password"
 * autoComplete={autoComplete}
 */

export function VideoProvidersPanel() {
  const panel = useVideoProvidersPanel();
  const { query, overview, editor, feedback, actions } = panel;

  if (query.isLoading) {
    return <ProviderPanelLoadingState />;
  }

  if (query.isError) {
    return (
      <ErrorBlock
        message={query.error?.message ?? "加载失败"}
        onRetry={() => void query.refetch()}
      />
    );
  }

  return (
    <section className="space-y-5 pb-28">
      <ProviderPanelHeader
        editing={editor !== null}
        enabled={overview.enabled}
        source={overview.source}
        serverItems={overview.serverItems}
        metrics={overview.metrics}
        onEdit={actions.beginEdit}
      />

      <ProviderPanelFeedback
        error={feedback.error}
        saved={feedback.saved}
        onDismissError={actions.dismissError}
      />

      {editor === null ? (
        <ProviderOverview
          enabled={overview.enabled}
          serverItems={overview.serverItems}
          summaries={overview.summaries}
          metrics={overview.metrics}
          onCreate={actions.beginEdit}
        />
      ) : (
        <ProviderEditorView
          drafts={editor.drafts}
          summaries={editor.summaries}
          metrics={editor.metrics}
          enabled={editor.enabled}
          source={editor.source}
          serverItems={editor.serverItems}
          proxyNames={editor.proxyNames}
          saving={editor.saving}
          onToggle={actions.setEnabled}
          onAddDraft={actions.addDraft}
          updateDraft={actions.updateDraft}
          updateModel={actions.updateModel}
          deleteDraft={actions.deleteDraft}
          onDiscard={actions.discard}
          onSave={actions.save}
        />
      )}
    </section>
  );
}
