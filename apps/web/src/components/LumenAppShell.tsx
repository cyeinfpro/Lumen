"use client";

import dynamic from "next/dynamic";
import { useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { usePathname } from "next/navigation";
import { useUiStore } from "@/store/useUiStore";
import { useInpaintStore } from "@/store/useInpaintStore";

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
import { Button, ErrorState, ToastViewport } from "@/components/ui/primitives";
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

type Props = {
  children: ReactNode;
  initialRuntimeDefaults: RuntimeDefaults;
};

function OptionalIsland({ children, title, resetKeys, recoveryRoot, onDismiss, dismissDisabled }: {
  children: ReactNode;
  title: string;
  resetKeys: readonly unknown[];
  recoveryRoot: HTMLDivElement | null;
  onDismiss?: () => void;
  dismissDisabled?: boolean;
}) {
  return (
    <ErrorBoundary
      resetKeys={resetKeys}
      fallback={(reset) => {
        const recovery = <ErrorState
          title={`${title}不可用`}
          description="重试此功能；模块加载失败时可刷新页面，未保存的编辑可能丢失。"
          onRetry={reset}
          retryLabel={`重试${title}`}
          className="bg-[var(--bg-1)] px-4 py-3 max-sm:py-3"
          secondaryAction={
            <>
              {onDismiss ? (
                <Button variant="secondary" size="sm" onClick={onDismiss} disabled={dismissDisabled}>
                  关闭
                </Button>
              ) : null}
              <Button variant="secondary" size="sm" onClick={() => window.location.reload()}>
                刷新页面
              </Button>
            </>
          }
        />;
        return recoveryRoot ? createPortal(recovery, recoveryRoot) : recovery;
      }}
    >
      {children}
    </ErrorBoundary>
  );
}

export function LumenAppShell({ children, initialRuntimeDefaults }: Props) {
  const pathname = usePathname();
  const [recoveryRoot, setRecoveryRoot] = useState<HTMLDivElement | null>(null);
  const lightboxOpen = useUiStore((state) => state.lightbox.open);
  const lightboxId = useUiStore((state) => state.lightbox.imageId);
  const lightboxEpoch = useUiStore((state) => state.lightbox.identityEpoch);
  const closeLightbox = useUiStore((state) => state.closeLightbox);
  const taskTrayMinimized = useUiStore((state) => state.taskTray.minimized);
  const inpaintOpen = useInpaintStore((state) => state.open);
  const inpaintId = useInpaintStore((state) => state.source?.imageId);
  const inpaintEpoch = useInpaintStore((state) => state.identityEpoch);
  const closeInpaint = useInpaintStore((state) => state.close);
  const inpaintSubmitting = useInpaintStore((state) => state.submitting);
  return (
    <QueryProvider>
      <RuntimeDefaultsBootstrap defaults={initialRuntimeDefaults} />
      <SSEProvider>
        <ErrorBoundary resetKeys={[pathname]}>
          <a
            href="#lumen-workspace"
            className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[var(--z-dialog)] focus:rounded-[var(--radius-control)] focus:bg-[var(--bg-1)] focus:px-4 focus:py-3 focus:text-[var(--fg-0)]"
            onClick={(event) => {
              const main = document.querySelector<HTMLElement>("[data-lumen-app-shell] main") ??
                document.getElementById("lumen-workspace");
              if (!main) return;
              event.preventDefault();
              main.tabIndex = -1;
              main.focus({ preventScroll: true });
            }}
          >
            跳到工作区
          </a>
          <div id="lumen-workspace" tabIndex={-1} data-lumen-app-shell className="flex min-h-0 min-w-0 flex-col">
            <PageTransitions>{children}</PageTransitions>
          </div>
        </ErrorBoundary>
      </SSEProvider>

      <div ref={setRecoveryRoot} data-island-recovery className="fixed right-[max(12px,env(safe-area-inset-right,0px))] bottom-[calc(var(--mobile-tabbar-height,56px)+12px)] z-[var(--z-dialog)] flex max-h-[50dvh] w-[calc(100%-24px)] max-w-sm flex-col gap-2 overflow-y-auto md:bottom-4" />
      <OptionalIsland title="图片预览" recoveryRoot={recoveryRoot} onDismiss={() => closeLightbox()} resetKeys={[pathname, lightboxOpen, lightboxId, lightboxEpoch]}>
        <Lightbox />
      </OptionalIsland>
      <OptionalIsland title="局部编辑" recoveryRoot={recoveryRoot} onDismiss={closeInpaint} dismissDisabled={inpaintSubmitting} resetKeys={[pathname, inpaintOpen, inpaintId, inpaintEpoch]}>
        <InpaintModal />
      </OptionalIsland>
      <OptionalIsland title="任务列表" recoveryRoot={recoveryRoot} resetKeys={[pathname, taskTrayMinimized, lightboxEpoch]}>
        <GlobalTaskTray />
      </OptionalIsland>
      <OptionalIsland title="命令面板" recoveryRoot={recoveryRoot} resetKeys={[pathname]}>
        <CommandPalette />
      </OptionalIsland>

      <SystemUpgradeBanner />
      <OfflineBanner />
      <ToastViewport />

      <IdleRouteWarmup />
      <ServiceWorkerRegister />
    </QueryProvider>
  );
}
