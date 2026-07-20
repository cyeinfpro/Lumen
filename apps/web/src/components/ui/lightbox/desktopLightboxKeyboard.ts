import { useEffect, type RefObject } from "react";

import { ZOOM_STEP, type ViewMode } from "./desktopLightboxModel";

type DesktopLightboxKeyboardActions = {
  close: () => void;
  download: () => void;
  iterate: () => void;
  toggleDetails: () => void;
  resetView: () => void;
  setViewMode: (viewMode: ViewMode) => void;
  setZoom: (update: (zoom: number) => number) => void;
  gotoDelta: (delta: 1 | -1) => void;
};

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

function trapDialogFocus(
  event: KeyboardEvent,
  root: HTMLElement | null,
): void {
  if (!root) return;
  const focusables = Array.from(
    root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  ).filter((element) => !element.hasAttribute("data-focus-skip"));
  if (focusables.length === 0) {
    event.preventDefault();
    return;
  }

  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (event.shiftKey && (active === first || !root.contains(active))) {
    event.preventDefault();
    last.focus();
    return;
  }
  if (!event.shiftKey && active === last) {
    event.preventDefault();
    first.focus();
  }
}

function shortcutHandlers(
  actions: DesktopLightboxKeyboardActions,
): Record<string, () => void> {
  return {
    d: actions.download,
    e: actions.iterate,
    i: actions.toggleDetails,
    "0": actions.resetView,
    "1": () => actions.setViewMode("fit"),
    f: () => actions.setViewMode("fit"),
    "2": () => actions.setViewMode("actual"),
    a: () => actions.setViewMode("actual"),
    "3": () => actions.setViewMode("fill"),
    "+": () => actions.setZoom((zoom) => zoom + ZOOM_STEP),
    "=": () => actions.setZoom((zoom) => zoom + ZOOM_STEP),
    "-": () => actions.setZoom((zoom) => zoom - ZOOM_STEP),
    _: () => actions.setZoom((zoom) => zoom - ZOOM_STEP),
    j: () => actions.gotoDelta(1),
    ArrowRight: () => actions.gotoDelta(1),
    k: () => actions.gotoDelta(-1),
    ArrowLeft: () => actions.gotoDelta(-1),
  };
}

function handleDesktopLightboxKeyDown(
  event: KeyboardEvent,
  root: HTMLElement | null,
  actions: DesktopLightboxKeyboardActions,
): void {
  if (event.key === "Escape") {
    actions.close();
    return;
  }
  if (event.key === "Tab") {
    trapDialogFocus(event, root);
    return;
  }
  const tag = (event.target as HTMLElement | null)?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return;

  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  const handler = shortcutHandlers(actions)[key];
  if (!handler) return;
  event.preventDefault();
  handler();
}

export function useDesktopLightboxKeyboard(
  open: boolean,
  containerRef: RefObject<HTMLDivElement | null>,
  actions: DesktopLightboxKeyboardActions,
): void {
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      handleDesktopLightboxKeyDown(
        event,
        containerRef.current,
        actions,
      );
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [actions, containerRef, open]);
}
