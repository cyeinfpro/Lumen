"use client";

import { RefreshCw, X } from "lucide-react";
import { useCallback, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import type { CanvasOutput } from "@/lib/canvas/types";
import { cn } from "@/lib/utils";
import { Button, IconButton } from "@/components/ui/primitives";
import {
  useModalLayer,
  usePortalReady,
} from "@/components/ui/primitives/mobile/useModalLayer";
import { CanvasOutputDownloadButton } from "./CanvasOutputDownloadButton";

export function CanvasVideoPreviewDialog({
  open,
  output,
  src,
  poster,
  title,
  onClose,
}: {
  open: boolean;
  output: CanvasOutput;
  src: string;
  poster?: string | null;
  title: string;
  onClose: () => void;
}) {
  const portalReady = usePortalReady();
  const headingId = useId();
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [loadFailed, setLoadFailed] = useState(false);
  const closeDialog = useCallback(() => onClose(), [onClose]);
  useBodyScrollLock(open);
  const onDialogKeyDown = useModalLayer({
    open,
    rootRef: dialogRef,
    onClose: closeDialog,
    initialFocusRef: closeButtonRef,
  });

  if (!open || !portalReady) return null;
  return createPortal(
    <div className="mobile-dialog-shell fixed inset-0 z-[var(--z-dialog)] flex items-end justify-center sm:items-center sm:p-5">
      <button
        type="button"
        aria-label="关闭视频预览"
        tabIndex={-1}
        className="absolute inset-0 cursor-default bg-[var(--surface-scrim)]"
        onClick={closeDialog}
      />
      <section
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
        className="mobile-dialog-panel surface-dialog relative flex max-h-[92dvh] w-full max-w-5xl flex-col overflow-hidden max-sm:rounded-t-[var(--radius-sheet)] max-sm:rounded-b-none max-sm:border-b-0"
      >
        <header className="flex shrink-0 items-center gap-3 border-b border-[var(--border)] px-4 py-3 sm:px-5">
          <div className="min-w-0 flex-1">
            <p className="type-page-kicker">视频预览</p>
            <h2 id={headingId} className="truncate type-card-title">
              {title}
            </h2>
          </div>
          <IconButton
            ref={closeButtonRef}
            aria-label="关闭视频预览"
            size="lg"
            onClick={closeDialog}
          >
            <X className="h-4 w-4" aria-hidden />
          </IconButton>
        </header>

        <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto p-3 sm:p-5">
          <div className="relative grid min-h-[220px] place-items-center overflow-hidden rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--surface-media)] sm:min-h-[420px]">
            <video
              key={`${src}:${loadAttempt}`}
              controls
              autoPlay
              playsInline
              preload="metadata"
              src={src}
              poster={poster || undefined}
              className={cn(
                "max-h-[72dvh] w-full object-contain",
                loadFailed && "invisible",
              )}
              onCanPlay={() => setLoadFailed(false)}
              onError={() => setLoadFailed(true)}
            />
            {loadFailed ? (
              <div
                role="alert"
                className="absolute inset-0 grid place-items-center p-6 text-center"
              >
                <div>
                  <p className="type-body-sm font-medium text-[var(--fg-1)]">
                    视频载入失败
                  </p>
                  <Button
                    size="sm"
                    variant="outline"
                    className="mt-3"
                    leftIcon={<RefreshCw className="h-4 w-4" aria-hidden />}
                    onClick={() => {
                      setLoadFailed(false);
                      setLoadAttempt((value) => value + 1);
                    }}
                  >
                    重新载入
                  </Button>
                </div>
              </div>
            ) : null}
          </div>
        </div>

        <footer className="mobile-dialog-footer flex shrink-0 items-center justify-between gap-3 border-t border-[var(--border)] px-4 py-3 sm:px-5">
          <p className="min-w-0 truncate type-caption text-[var(--fg-2)]">
            {videoMetadataLabel(output)}
          </p>
          <CanvasOutputDownloadButton
            output={output}
            title={title}
            presentation="button"
            className="shrink-0"
          />
        </footer>
      </section>
    </div>,
    document.body,
  );
}

function videoMetadataLabel(output: CanvasOutput): string {
  const width = Number(output.width);
  const height = Number(output.height);
  if (
    Number.isFinite(width) &&
    Number.isFinite(height) &&
    width > 0 &&
    height > 0
  ) {
    return `${Math.round(width)} x ${Math.round(height)}`;
  }
  return output.label?.trim() || "画布视频成品";
}
