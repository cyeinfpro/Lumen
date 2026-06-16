"use client";

import dynamic from "next/dynamic";
import type { ReactNode } from "react";

import { ErrorBoundary } from "@/components/ErrorBoundary";
import { IdleRouteWarmup } from "@/components/IdleRouteWarmup";
import { OfflineBanner } from "@/components/OfflineBanner";
import { QueryProvider } from "@/components/QueryProvider";
import {
  RuntimeDefaultsBootstrap,
  type RuntimeDefaults,
} from "@/components/RuntimeDefaultsBootstrap";
import { ServiceWorkerRegister } from "@/components/ServiceWorkerRegister";
import { SSEProvider } from "@/components/SSEProvider";
import { SystemUpgradeBanner } from "@/components/SystemUpgradeBanner";
import { MobileToastViewport } from "@/components/ui/primitives/mobile/Toast";
import { ToastViewport } from "@/components/ui/primitives";
import { PageTransitions } from "@/components/ui/shell/PageTransitions";

const Lightbox = dynamic(
  () => import("@/components/ui/Lightbox").then((mod) => mod.Lightbox),
  { ssr: false, loading: () => null },
);

const InpaintModal = dynamic(
  () =>
    import("@/components/ui/inpaint/LazyInpaintModal").then(
      (mod) => mod.LazyInpaintModal,
    ),
  { ssr: false, loading: () => null },
);

const GlobalTaskTray = dynamic(
  () =>
    import("@/components/ui/GlobalTaskTray").then((mod) => mod.GlobalTaskTray),
  { ssr: false, loading: () => null },
);

const CommandPalette = dynamic(
  () =>
    import("@/components/ui/CommandPalette").then((mod) => mod.CommandPalette),
  { ssr: false, loading: () => null },
);

const DesktopBootstrapGate = dynamic(
  () =>
    import("@/components/desktop/DesktopBootstrapGate").then(
      (mod) => mod.DesktopBootstrapGate,
    ),
  { ssr: false, loading: () => null },
);

type Props = {
  children: ReactNode;
  initialRuntimeDefaults: RuntimeDefaults;
};

function OptionalIsland({ children }: { children: ReactNode }) {
  return <ErrorBoundary fallback={null}>{children}</ErrorBoundary>;
}

export function LumenAppShell({ children, initialRuntimeDefaults }: Props) {
  return (
    <QueryProvider>
      <RuntimeDefaultsBootstrap defaults={initialRuntimeDefaults} />
      <DesktopBootstrapGate />
      <SSEProvider>
        <ErrorBoundary>
          <PageTransitions>{children}</PageTransitions>
        </ErrorBoundary>
      </SSEProvider>

      <OptionalIsland>
        <Lightbox />
      </OptionalIsland>
      <OptionalIsland>
        <InpaintModal />
      </OptionalIsland>
      <OptionalIsland>
        <GlobalTaskTray />
      </OptionalIsland>

      <SystemUpgradeBanner />
      <OfflineBanner />
      <ToastViewport />
      <MobileToastViewport />

      <OptionalIsland>
        <CommandPalette />
      </OptionalIsland>
      <IdleRouteWarmup />
      <ServiceWorkerRegister />
    </QueryProvider>
  );
}
