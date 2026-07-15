import { useCallback, useEffect, useRef, useState } from "react";

import type { CanvasEditorStore } from "@/lib/canvas/store";
import type { CanvasViewportApi } from "./CanvasViewport";

export function useCanvasKeyboardShortcuts(
  store: CanvasEditorStore,
  viewportApi: CanvasViewportApi | null,
  actions: CanvasShortcutActions,
) {
  const actionsRef = useRef(actions);
  useEffect(() => {
    actionsRef.current = actions;
  }, [actions]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const target = event.target as HTMLElement | null;
      if (
        target?.matches("input, textarea, select, [contenteditable='true']") ||
        target?.closest("[role='dialog']")
      ) {
        return;
      }
      if (
        hasModifier(event) &&
        event.key.toLowerCase() === "v" &&
        target?.closest("[data-canvas-native-paste]")
      ) {
        return;
      }
      const bindings = createCanvasShortcutBindings(
        store,
        viewportApi,
        actionsRef.current,
      );
      const shortcut = bindings.find((item) => item.matches(event));
      if (!shortcut) return;
      event.preventDefault();
      shortcut.run(event);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [store, viewportApi]);
}

interface CanvasShortcutActions {
  onEscape: () => void;
  onOpenCommandMenu: () => void;
  onOpenShortcuts: () => void;
  onCopy: () => void | Promise<unknown>;
  onPaste: () => void | Promise<unknown>;
  onDuplicate: () => void;
  onAutoLayout: () => void;
  onFitSelection: () => void;
  onRunSelected: () => void;
  onToggleGrid: () => void;
  onToggleMiniMap: () => void;
}

interface CanvasShortcutBinding {
  matches: (event: KeyboardEvent) => boolean;
  run: (event: KeyboardEvent) => void;
}

function createCanvasShortcutBindings(
  store: CanvasEditorStore,
  viewportApi: CanvasViewportApi | null,
  actions: CanvasShortcutActions,
): CanvasShortcutBinding[] {
  return [
    binding(modifiedShiftedKey("k"), actions.onOpenCommandMenu),
    binding(modifiedKey("z"), (event) => {
      if (event.shiftKey) store.getState().redo();
      else store.getState().undo();
    }),
    binding(modifiedKey("y"), () => store.getState().redo()),
    binding(modifiedKey("0"), () => viewportApi?.fitView()),
    binding(modifiedKey("a"), () => {
      const state = store.getState();
      state.selectNodes(state.graph.nodes.map((node) => node.id));
    }),
    binding(modifiedKey("c"), () => void actions.onCopy()),
    binding(modifiedKey("v"), () => void actions.onPaste()),
    binding(modifiedKey("d"), actions.onDuplicate),
    binding(
      (event) => hasModifier(event) && event.key === "Enter",
      actions.onRunSelected,
    ),
    binding(shiftedKey("2"), actions.onFitSelection),
    binding(shiftedKey("a"), actions.onAutoLayout),
    binding(
      (event) => !hasModifier(event) && !event.shiftKey && event.key === "/",
      actions.onOpenCommandMenu,
    ),
    binding((event) => event.key === "?", actions.onOpenShortcuts),
    binding(plainKey("g"), actions.onToggleGrid),
    binding(plainKey("m"), actions.onToggleMiniMap),
    binding(
      (event) => event.key === "+" || event.key === "=",
      () => viewportApi?.zoomIn(),
    ),
    binding(plainKey("-"), () => viewportApi?.zoomOut()),
    binding(plainKey("0"), () => viewportApi?.resetZoom()),
    binding(plainKey("escape"), actions.onEscape),
  ];
}

function binding(
  matches: CanvasShortcutBinding["matches"],
  run: CanvasShortcutBinding["run"],
): CanvasShortcutBinding {
  return { matches, run };
}

function modifiedKey(key: string) {
  return (event: KeyboardEvent) =>
    hasModifier(event) && event.key.toLowerCase() === key;
}

function shiftedKey(key: string) {
  return (event: KeyboardEvent) =>
    !hasModifier(event) && event.shiftKey && event.key.toLowerCase() === key;
}

function modifiedShiftedKey(key: string) {
  return (event: KeyboardEvent) =>
    hasModifier(event) && event.shiftKey && event.key.toLowerCase() === key;
}

function plainKey(key: string) {
  return (event: KeyboardEvent) =>
    !hasModifier(event) && !event.shiftKey && event.key.toLowerCase() === key;
}

function hasModifier(event: KeyboardEvent): boolean {
  return event.metaKey || event.ctrlKey;
}

export function useCanvasFullscreen() {
  const [fullscreen, setFullscreen] = useState(false);
  const ownsNativeFullscreenRef = useRef(false);

  const exitFullscreen = useCallback(async () => {
    setFullscreen(false);
    if (
      ownsNativeFullscreenRef.current &&
      document.fullscreenElement &&
      typeof document.exitFullscreen === "function"
    ) {
      await document.exitFullscreen().catch(() => undefined);
    }
    ownsNativeFullscreenRef.current = false;
  }, []);

  const toggleFullscreen = useCallback(async () => {
    if (fullscreen) {
      await exitFullscreen();
      return;
    }
    setFullscreen(true);
    const target = document.documentElement;
    if (typeof target.requestFullscreen !== "function") return;
    ownsNativeFullscreenRef.current = true;
    try {
      await target.requestFullscreen();
    } catch {
      ownsNativeFullscreenRef.current = false;
    }
  }, [exitFullscreen, fullscreen]);

  useEffect(() => {
    const onFullscreenChange = () => {
      if (
        fullscreen &&
        ownsNativeFullscreenRef.current &&
        !document.fullscreenElement
      ) {
        ownsNativeFullscreenRef.current = false;
        setFullscreen(false);
      }
    };
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () =>
      document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, [fullscreen]);

  useEffect(() => {
    if (!fullscreen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [fullscreen]);

  return { fullscreen, toggleFullscreen, exitFullscreen };
}
