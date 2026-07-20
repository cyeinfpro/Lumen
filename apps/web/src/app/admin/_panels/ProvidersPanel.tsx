"use client";

import { ProviderPanelView } from "./providers/panel";
import { useProviderPanelState } from "./providers/useProviderPanelState";

export function ProvidersPanel() {
  const state = useProviderPanelState();
  const { serverProxies } = state;
  return <ProviderPanelView state={state} proxies={serverProxies} />;
}
