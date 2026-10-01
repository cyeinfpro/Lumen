"use client";

import { useEffect, type RefObject } from "react";
import { useChatStore } from "@/store/useChatStore";

/** Retain explicit editing intent across history loading and shell remounts. */
export function useDesktopComposerExpansion(textareaRef: RefObject<HTMLTextAreaElement | null>) {
  const expanded = useChatStore((state) => state.composerExpanded);
  const setExpanded = useChatStore((state) => state.setComposerExpanded);

  useEffect(() => {
    const onExpand = () => {
      setExpanded(true);
      requestAnimationFrame(() => textareaRef.current?.focus());
    };
    window.addEventListener("lumen:composer-expand", onExpand);
    return () => window.removeEventListener("lumen:composer-expand", onExpand);
  }, [setExpanded, textareaRef]);

  useEffect(() => {
    if (!expanded) return;
    const raf = requestAnimationFrame(() => textareaRef.current?.focus({ preventScroll: true }));
    return () => cancelAnimationFrame(raf);
  }, [expanded, textareaRef]);

  return { expanded, setExpanded };
}
