"use client";

import { ProviderPanelView } from "./providers/panel";
import { useProviderPanelState } from "./providers/useProviderPanelState";
import { VoiceProviderPanel } from "./VoiceProviderPanel";

export function ProvidersPanel() {
  const state = useProviderPanelState();
  const { serverProxies } = state;
  return (
    <div className="space-y-5">
      <VoiceProviderPanel />
      <ProviderPanelView state={state} proxies={serverProxies} />
    </div>
  );
}
