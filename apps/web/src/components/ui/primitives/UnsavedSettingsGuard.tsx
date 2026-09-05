"use client";

import { useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/ui/primitives";

const NAVIGATION_EVENT = "lumen:settings-navigation";

/** Routes non-link settings navigation through the mounted editor's guard. */
export function requestSettingsNavigation(action: () => void): void {
  const event = new CustomEvent(NAVIGATION_EVENT, { cancelable: true, detail: action });
  if (window.dispatchEvent(event)) action();
}

function isModifiedNavigation(event: MouseEvent) {
  return event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
}

/** Keeps explicit-save drafts in their existing editor until departure is confirmed. */
export function useUnsavedSettingsGuard(dirty: boolean) {
  const [pending, setPending] = useState<(() => void) | null>(null);
  const bypass = useRef(false);
  const requestNavigation = (action: () => void) => {
    if (dirty) setPending(() => action);
    else action();
  };

  useEffect(() => {
    if (!dirty) return;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    const onNavigation = (event: Event) => {
      if (bypass.current || event.defaultPrevented) return;
      event.preventDefault();
      setPending(() => (event as CustomEvent<() => void>).detail);
    };
    const onClick = (event: MouseEvent) => {
      if (bypass.current || isModifiedNavigation(event)) return;
      const element = event.target instanceof Element
        ? event.target.closest<HTMLElement>("a[href], [data-settings-navigation]")
        : null;
      if (!element) return;
      if (element instanceof HTMLAnchorElement) {
        if (element.target === "_blank" || element.hasAttribute("download")) return;
        const url = new URL(element.href, window.location.href);
        if (url.origin === window.location.origin && url.pathname === window.location.pathname && url.search === window.location.search) return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      setPending(() => () => { if (element.isConnected) element.click(); });
    };
    window.addEventListener("beforeunload", beforeUnload);
    window.addEventListener(NAVIGATION_EVENT, onNavigation);
    document.addEventListener("click", onClick, true);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      window.removeEventListener(NAVIGATION_EVENT, onNavigation);
      document.removeEventListener("click", onClick, true);
    };
  }, [dirty]);

  const dialog = <ConfirmDialog
    open={dirty && pending !== null}
    onOpenChange={(open) => { if (!open) setPending(null); }}
    title="放弃未保存的设置？"
    description="离开后，本页未保存的修改将丢失。"
    confirmText="放弃并离开"
    cancelText="继续编辑"
    onConfirm={() => {
      setPending(null);
      bypass.current = true;
      try { pending?.(); }
      finally { bypass.current = false; }
    }}
  />;
  return { requestNavigation, dialog };
}

export function UnsavedSettingsGuard({ dirty }: { dirty: boolean }) {
  return useUnsavedSettingsGuard(dirty).dialog;
}
