"use client";

import { Loader2, Upload } from "lucide-react";
import {
  type ClipboardEvent,
  type DragEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { uploadImage } from "@/lib/apiClient";
import type { CanvasNodeDefinition } from "@/lib/canvas/types";
import {
  MAX_UPLOAD_SOURCE_BYTES,
  maxUploadSourceMessage,
} from "@/lib/uploadLimits";
import { cn } from "@/lib/utils";
import { Button, IconButton, toast } from "@/components/ui/primitives";

const IMAGE_ACCEPT = "image/png,image/jpeg,image/webp";
const ALLOWED_IMAGE_MIME = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
]);
const ALLOWED_IMAGE_EXTENSION = /\.(?:png|jpe?g|webp)$/i;

export function CanvasImageAssetDropZone({
  nodeId,
  config,
  editingEnabled,
  onUpdateConfig,
  children,
}: {
  nodeId: string;
  config: CanvasNodeDefinition["config"];
  editingEnabled: boolean;
  onUpdateConfig?: (
    nodeId: string,
    config: Record<string, unknown>,
  ) => void;
  children?: ReactNode;
}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragDepthRef = useRef(0);
  const requestRef = useRef<CanvasImageUploadRequest | null>(null);
  const latestRef = useRef({ config, nodeId, onUpdateConfig });
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState(false);
  const disabled = !editingEnabled || !onUpdateConfig;
  const hasPreview = Boolean(children);

  useEffect(() => {
    latestRef.current = { config, nodeId, onUpdateConfig };
  }, [config, nodeId, onUpdateConfig]);

  useEffect(
    () => () => {
      requestRef.current?.controller.abort();
      requestRef.current = null;
    },
    [],
  );

  const uploadFile = useCallback(async (file: File) => {
    const validationError = canvasImageUploadError(file);
    if (validationError) {
      toast.error(validationError);
      return;
    }
    const latest = latestRef.current;
    if (!latest.onUpdateConfig) return;
    requestRef.current?.controller.abort();
    const request = {
      controller: new AbortController(),
      nodeId: latest.nodeId,
      initialImageId: latest.config.image_id,
      initialDisplayName: latest.config.display_name,
    };
    requestRef.current = request;
    setUploading(true);
    try {
      const image = await uploadImage(file, {
        signal: request.controller.signal,
      });
      const current = latestRef.current;
      if (
        requestRef.current !== request ||
        current.nodeId !== request.nodeId
      ) {
        return;
      }
      if (
        !Object.is(current.config.image_id, request.initialImageId) ||
        !Object.is(
          current.config.display_name,
          request.initialDisplayName,
        )
      ) {
        toast.info("上传已完成，但节点内容已被修改，未自动覆盖。");
        return;
      }
      current.onUpdateConfig?.(request.nodeId, {
        ...current.config,
        image_id: image.id,
        display_name: file.name,
      });
      toast.success("图片已上传");
    } catch (error) {
      if (!request.controller.signal.aborted) {
        toast.error(error instanceof Error ? error.message : "图片上传失败");
      }
    } finally {
      if (requestRef.current === request) {
        requestRef.current = null;
        setUploading(false);
      }
    }
  }, []);

  const ingestTransfer = useCallback(
    (transfer: DataTransfer | null) => {
      const files = imageFilesFromTransfer(transfer);
      if (files.length === 0) return false;
      if (files.length > 1) {
        toast.info("图片素材节点仅保留第一张图片。");
      }
      void uploadFile(files[0]);
      return true;
    },
    [uploadFile],
  );

  const resetDrag = useCallback(() => {
    dragDepthRef.current = 0;
    setDragActive(false);
  }, []);

  const onPaste = useCallback(
    (event: ClipboardEvent<HTMLDivElement>) => {
      if (disabled || !ingestTransfer(event.clipboardData)) return;
      event.preventDefault();
      event.stopPropagation();
    },
    [disabled, ingestTransfer],
  );

  const onDragEnter = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      if (disabled || !hasImageTransfer(event.dataTransfer)) return;
      event.preventDefault();
      event.stopPropagation();
      dragDepthRef.current += 1;
      setDragActive(true);
    },
    [disabled],
  );

  const onDragOver = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      if (disabled || !hasImageTransfer(event.dataTransfer)) return;
      event.preventDefault();
      event.stopPropagation();
      event.dataTransfer.dropEffect = "copy";
    },
    [disabled],
  );

  const onDragLeave = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!dragActive) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragActive(false);
  }, [dragActive]);

  const onDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      if (disabled || !hasImageTransfer(event.dataTransfer)) return;
      event.preventDefault();
      event.stopPropagation();
      resetDrag();
      if (!ingestTransfer(event.dataTransfer)) {
        toast.error("仅支持 PNG、JPG 或 WebP 图片");
      }
    },
    [disabled, ingestTransfer, resetDrag],
  );

  const openPicker = useCallback(() => {
    if (!disabled && !uploading) fileInputRef.current?.click();
  }, [disabled, uploading]);

  return (
    <div
      data-canvas-image-dropzone
      data-canvas-native-paste={disabled ? undefined : ""}
      tabIndex={disabled ? -1 : 0}
      aria-label="图片素材上传区域，可粘贴或拖入图片"
      aria-busy={uploading || undefined}
      onPaste={onPaste}
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      onKeyDown={(event) => {
        if (
          event.target === event.currentTarget &&
          (event.key === "Enter" || event.key === " ")
        ) {
          event.preventDefault();
          openPicker();
        }
      }}
      className={cn(
        "nodrag nopan nowheel relative min-h-[112px] overflow-hidden bg-[var(--surface-media)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]",
        dragActive && "ring-2 ring-inset ring-[var(--accent)]",
      )}
    >
      <input
        ref={fileInputRef}
        type="file"
        accept={IMAGE_ACCEPT}
        tabIndex={-1}
        className="hidden"
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = "";
          if (file) void uploadFile(file);
        }}
      />

      {hasPreview ? (
        <>
          {children}
          <IconButton
            size="sm"
            variant="ghost"
            disabled={disabled || uploading}
            aria-label="替换图片素材"
            title="替换图片"
            onPointerDown={(event) => event.stopPropagation()}
            onClick={(event) => {
              event.stopPropagation();
              openPicker();
            }}
            className="nodrag nopan absolute left-2 top-2 z-10 bg-[var(--media-control-bg)] text-[var(--media-control-fg)] shadow-[var(--shadow-2)] hover:bg-[var(--media-control-bg)] hover:brightness-110"
          >
            <Upload className="h-4 w-4" aria-hidden />
          </IconButton>
        </>
      ) : (
        <div className="grid min-h-[112px] place-items-center p-3 text-center">
          <div className="grid justify-items-center gap-2">
            <Upload
              className="h-6 w-6 text-[var(--accent)]"
              aria-hidden
            />
            <Button
              size="sm"
              variant="primary"
              disabled={disabled || uploading}
              onPointerDown={(event) => event.stopPropagation()}
              onClick={(event) => {
                event.stopPropagation();
                openPicker();
              }}
            >
              上传图片
            </Button>
            <span className="type-mono-meta text-[var(--media-control-fg)] opacity-70">
              PNG · JPG · WEBP
            </span>
          </div>
        </div>
      )}

      {dragActive || uploading ? (
        <div
          role="status"
          className="pointer-events-none absolute inset-0 z-20 grid place-items-center border-2 border-dashed border-[var(--accent)] bg-[var(--bg-0)]/88 p-4 text-center"
        >
          <div className="grid justify-items-center gap-2">
            {uploading ? (
              <Loader2
                className="h-6 w-6 animate-spin text-[var(--accent)] motion-reduce:animate-none"
                aria-hidden
              />
            ) : (
              <Upload className="h-6 w-6 text-[var(--accent)]" aria-hidden />
            )}
            <span className="type-body-sm font-medium text-[var(--fg-1)]">
              {uploading ? "图片上传中" : "松开上传图片"}
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}

interface CanvasImageUploadRequest {
  controller: AbortController;
  nodeId: string;
  initialImageId: unknown;
  initialDisplayName: unknown;
}

function canvasImageUploadError(file: File): string | null {
  if (file.size <= 0) return "图片文件为空";
  if (file.size > MAX_UPLOAD_SOURCE_BYTES) return maxUploadSourceMessage();
  if (
    ALLOWED_IMAGE_MIME.has(file.type.toLowerCase()) ||
    (!file.type && ALLOWED_IMAGE_EXTENSION.test(file.name))
  ) {
    return null;
  }
  return "仅支持 PNG、JPG 或 WebP 图片";
}

function imageFilesFromTransfer(transfer: DataTransfer | null): File[] {
  if (!transfer) return [];
  const files: File[] = [];
  if (transfer.items?.length) {
    for (const item of Array.from(transfer.items)) {
      if (item.kind !== "file") continue;
      const file = item.getAsFile();
      if (file && isImageCandidate(file)) files.push(file);
    }
  }
  if (files.length > 0) return files;
  return Array.from(transfer.files ?? []).filter(isImageCandidate);
}

function hasImageTransfer(transfer: DataTransfer | null): boolean {
  if (!transfer) return false;
  if (transfer.items?.length) {
    return Array.from(transfer.items).some(
      (item) =>
        item.kind === "file" &&
        (!item.type || item.type.startsWith("image/")),
    );
  }
  return Array.from(transfer.files ?? []).some(isImageCandidate);
}

function isImageCandidate(file: File): boolean {
  return (
    file.type.startsWith("image/") ||
    (!file.type && ALLOWED_IMAGE_EXTENSION.test(file.name))
  );
}
