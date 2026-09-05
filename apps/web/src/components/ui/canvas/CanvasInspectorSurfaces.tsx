"use client";

import { X } from "lucide-react";

import { IconButton } from "@/components/ui/primitives";
import { BottomSheet } from "@/components/ui/primitives/mobile";
import {
  CanvasInspector,
  type CanvasInspectorProps,
} from "./CanvasInspector";

export function CanvasInspectorSurfaces({
  isMobile,
  isCompact,
  open,
  hasSelection,
  onClose,
  onCloseMobile,
  ...inspectorProps
}: CanvasInspectorProps & {
  isMobile: boolean;
  isCompact: boolean;
  open: boolean;
  hasSelection: boolean;
  onClose: () => void;
  onCloseMobile: () => void;
}) {
  const showTabletInspector = !isMobile && isCompact && open;

  return (
    <>
      {hasSelection ? (
        <aside className="hidden min-h-0 border-l border-[var(--border)] bg-[var(--bg-1)] min-[1200px]:block">
          <CanvasInspector {...inspectorProps} />
        </aside>
      ) : null}

      {showTabletInspector ? (
        <div className="pointer-events-none absolute inset-0 z-[var(--z-dialog)] flex justify-end p-3">
          <aside className="pointer-events-auto relative flex h-full w-[min(352px,calc(100vw-24px))] min-h-0 flex-col border border-[var(--border)] bg-[var(--bg-1)] shadow-[var(--shadow-2)]">
            <IconButton
              aria-label="关闭检查器"
              size="lg"
              onClick={onClose}
              className="absolute right-2 top-2 z-[var(--z-tabbar)]"
            >
              <X className="h-4 w-4" />
            </IconButton>
            <CanvasInspector {...inspectorProps} />
          </aside>
        </div>
      ) : null}

      {isMobile ? (
        <BottomSheet
          open={open}
          onClose={onCloseMobile}
          ariaLabel="节点检查器"
          snapPoints={["88%"]}
          className="mobile-dialog-sheet"
        >
          <div className="relative h-full min-h-0">
            <IconButton
              aria-label="关闭检查器"
              size="lg"
              onClick={onCloseMobile}
              className="absolute right-3 top-3 z-[var(--z-tabbar)]"
            >
              <X className="h-4 w-4" />
            </IconButton>
            <CanvasInspector {...inspectorProps} />
          </div>
        </BottomSheet>
      ) : null}
    </>
  );
}
