"use client";

import {
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
  useCallback,
  useEffect,
  useRef,
  useSyncExternalStore,
} from "react";

const MODAL_SELECTOR = '[role="dialog"][aria-modal="true"]';
const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "summary",
  "[contenteditable='true']",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

type ModalLayer = {
  id: symbol;
  root: HTMLElement;
};

const activeModalLayers: ModalLayer[] = [];
const pendingFocusRestoreTimers = new Set<number>();
const PORTAL_SUBSCRIBE = (): (() => void) => () => {};
const PORTAL_CLIENT_SNAPSHOT = (): true => true;
const PORTAL_SERVER_SNAPSHOT = (): false => false;

function cancelPendingFocusRestores() {
  for (const timer of pendingFocusRestoreTimers) {
    window.clearTimeout(timer);
  }
  pendingFocusRestoreTimers.clear();
}

function registerModalLayer(layer: ModalLayer) {
  const previousIndex = activeModalLayers.findIndex(
    (candidate) => candidate.id === layer.id,
  );
  if (previousIndex >= 0) activeModalLayers.splice(previousIndex, 1);
  activeModalLayers.push(layer);
  cancelPendingFocusRestores();
}

function unregisterModalLayer(id: symbol) {
  const index = activeModalLayers.findIndex((layer) => layer.id === id);
  if (index >= 0) activeModalLayers.splice(index, 1);
}

function topModalLayer(): ModalLayer | null {
  while (activeModalLayers.length > 0) {
    const layer = activeModalLayers.at(-1);
    if (layer?.root.isConnected) return layer;
    activeModalLayers.pop();
  }
  return null;
}

function isElementVisible(element: HTMLElement): boolean {
  if (
    element.closest("[inert], [aria-hidden='true'], [hidden]") ||
    element.getAttribute("aria-hidden") === "true"
  ) {
    return false;
  }
  return element.getClientRects().length > 0;
}

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => isElementVisible(element),
  );
}

function initialFocusTarget(
  root: HTMLElement,
  preferred: HTMLElement | null | undefined,
): HTMLElement {
  if (
    preferred?.isConnected &&
    root.contains(preferred) &&
    isElementVisible(preferred)
  ) {
    return preferred;
  }
  return (
    focusableElements(root).find(
      (element) =>
        !element.hasAttribute("data-autofocus-skip") &&
        !element.hasAttribute("data-focus-skip"),
    ) ?? root
  );
}

function focusModal(
  root: HTMLElement,
  preferred?: HTMLElement | null,
): void {
  initialFocusTarget(root, preferred).focus({ preventScroll: true });
}

function isTopmostModal(root: HTMLElement): boolean {
  const registeredLayer = topModalLayer();
  if (registeredLayer) return registeredLayer.root === root;

  const dialogs = Array.from(
    document.querySelectorAll<HTMLElement>(MODAL_SELECTOR),
  ).filter((dialog) => dialog.isConnected && isElementVisible(dialog));
  return dialogs.at(-1) === root;
}

function scheduleFocusRestore(
  previous: HTMLElement | null,
  timerRef: RefObject<number | null>,
) {
  if (timerRef.current !== null) {
    window.clearTimeout(timerRef.current);
    pendingFocusRestoreTimers.delete(timerRef.current);
  }
  const timer = window.setTimeout(() => {
    pendingFocusRestoreTimers.delete(timer);
    if (timerRef.current === timer) timerRef.current = null;

    const topLayer = topModalLayer();
    if (topLayer) {
      if (previous?.isConnected && topLayer.root.contains(previous)) {
        previous.focus({ preventScroll: true });
      } else {
        focusModal(topLayer.root);
      }
      return;
    }
    if (previous?.isConnected) previous.focus({ preventScroll: true });
  }, 0);
  timerRef.current = timer;
  pendingFocusRestoreTimers.add(timer);
}

export function usePortalReady(): boolean {
  return useSyncExternalStore(
    PORTAL_SUBSCRIBE,
    PORTAL_CLIENT_SNAPSHOT,
    PORTAL_SERVER_SNAPSHOT,
  );
}

export function trapModalFocus(
  event: Pick<
    ReactKeyboardEvent<HTMLElement>,
    "key" | "shiftKey" | "preventDefault"
  >,
  root: HTMLElement | null,
) {
  if (event.key !== "Tab" || !root) return;
  const focusables = focusableElements(root);
  if (focusables.length === 0) {
    event.preventDefault();
    root.focus({ preventScroll: true });
    return;
  }
  const first = focusables[0];
  const last = focusables[focusables.length - 1];
  const active = document.activeElement as HTMLElement | null;
  if (
    event.shiftKey &&
    (active === root || active === first || !root.contains(active))
  ) {
    event.preventDefault();
    last.focus({ preventScroll: true });
  } else if (
    !event.shiftKey &&
    (active === last || !root.contains(active))
  ) {
    event.preventDefault();
    first.focus({ preventScroll: true });
  }
}

export function useModalLayer<T extends HTMLElement>({
  open,
  rootRef,
  onClose,
  initialFocusRef,
  closeOnEscape = true,
  restoreFocus = true,
}: {
  open: boolean;
  rootRef: RefObject<T | null>;
  onClose: () => void;
  initialFocusRef?: RefObject<HTMLElement | null>;
  closeOnEscape?: boolean;
  restoreFocus?: boolean;
}) {
  const layerIdRef = useRef(Symbol("lumen-modal-layer"));
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const initialFocusTimerRef = useRef<number | null>(null);
  const restoreFocusTimerRef = useRef<number | null>(null);
  const configRef = useRef({
    closeOnEscape,
    initialFocusRef,
    onClose,
    restoreFocus,
  });

  useEffect(() => {
    configRef.current = {
      closeOnEscape,
      initialFocusRef,
      onClose,
      restoreFocus,
    };
  }, [closeOnEscape, initialFocusRef, onClose, restoreFocus]);

  useEffect(() => {
    if (!open) return;
    if (restoreFocusTimerRef.current !== null) {
      window.clearTimeout(restoreFocusTimerRef.current);
      pendingFocusRestoreTimers.delete(restoreFocusTimerRef.current);
      restoreFocusTimerRef.current = null;
    }

    const root = rootRef.current;
    if (!root) return;
    const layerId = layerIdRef.current;

    previousFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    registerModalLayer({
      id: layerId,
      root,
    });

    initialFocusTimerRef.current = window.setTimeout(() => {
      if (!isTopmostModal(root)) return;
      focusModal(root, configRef.current.initialFocusRef?.current);
    }, 0);

    const onKeyDown = (event: KeyboardEvent) => {
      const currentRoot = rootRef.current;
      if (!currentRoot || !isTopmostModal(currentRoot)) return;
      if (
        event.key === "Escape" &&
        !event.isComposing &&
        !event.repeat &&
        configRef.current.closeOnEscape
      ) {
        event.preventDefault();
        event.stopPropagation();
        configRef.current.onClose();
      }
    };
    const onFocusIn = (event: FocusEvent) => {
      const currentRoot = rootRef.current;
      if (
        !currentRoot ||
        !isTopmostModal(currentRoot) ||
        (event.target instanceof Node && currentRoot.contains(event.target))
      ) {
        return;
      }
      focusModal(currentRoot, configRef.current.initialFocusRef?.current);
    };

    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("focusin", onFocusIn, true);
    return () => {
      if (initialFocusTimerRef.current !== null) {
        window.clearTimeout(initialFocusTimerRef.current);
        initialFocusTimerRef.current = null;
      }
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("focusin", onFocusIn, true);
      unregisterModalLayer(layerId);
      if (!configRef.current.restoreFocus) return;
      scheduleFocusRestore(
        previousFocusRef.current,
        restoreFocusTimerRef,
      );
    };
  }, [open, rootRef]);

  return useCallback(
    (event: ReactKeyboardEvent<T>) => {
      if (!isTopmostModal(event.currentTarget)) return;
      trapModalFocus(event, rootRef.current);
    },
    [rootRef],
  );
}
