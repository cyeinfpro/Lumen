"use client";

import { Download } from "lucide-react";
import { useState } from "react";

import { triggerImageDownload } from "@/components/ui/lightbox/utils";
import {
  imageBinaryUrl,
  videoDownloadUrl,
} from "@/lib/apiClient";
import type { CanvasOutput } from "@/lib/canvas/types";
import { cn } from "@/lib/utils";
import { Button, IconButton, toast } from "@/components/ui/primitives";

type DownloadButtonPresentation = "icon" | "button";

export function CanvasOutputDownloadButton({
  output,
  title,
  presentation = "icon",
  className,
}: {
  output: CanvasOutput;
  title?: string;
  presentation?: DownloadButtonPresentation;
  className?: string;
}) {
  const [downloading, setDownloading] = useState(false);
  const source = canvasOutputDownloadSource(output);
  if (!source) return null;

  const label = output.type === "video" ? "下载视频成品" : "下载图片成品";
  const download = async () => {
    if (downloading) return;
    setDownloading(true);
    try {
      const result = await triggerCanvasOutputDownload(output, title);
      toast.success(
        result === "opened" ? "已打开成品原文件" : "已开始下载成品",
      );
    } catch {
      toast.error("成品下载失败");
    } finally {
      setDownloading(false);
    }
  };

  const interactionProps = {
    onPointerDown: (event: React.PointerEvent<HTMLButtonElement>) =>
      event.stopPropagation(),
    onDoubleClick: (event: React.MouseEvent<HTMLButtonElement>) =>
      event.stopPropagation(),
    onClick: (event: React.MouseEvent<HTMLButtonElement>) => {
      event.stopPropagation();
      void download();
    },
  };

  if (presentation === "button") {
    return (
      <Button
        size="sm"
        variant="outline"
        loading={downloading}
        className={className}
        leftIcon={<Download className="h-4 w-4" aria-hidden />}
        aria-label={label}
        data-canvas-output-download
        {...interactionProps}
      >
        下载原文件
      </Button>
    );
  }

  return (
    <IconButton
      size="sm"
      variant="ghost"
      loading={downloading}
      className={cn(
        "nodrag nopan nowheel bg-[var(--media-control-bg)] text-[var(--media-control-fg)] shadow-[var(--shadow-2)] hover:bg-[var(--media-control-bg)] hover:brightness-110",
        className,
      )}
      aria-label={label}
      title={label}
      data-canvas-output-download
      {...interactionProps}
    >
      <Download className="h-4 w-4" aria-hidden />
    </IconButton>
  );
}

export function canvasOutputDownloadSource(
  output: CanvasOutput,
): string | null {
  if (output.type === "image") {
    return (
      mediaText(output.image_id)
        ? imageBinaryUrl(String(output.image_id))
        : mediaText(output.url)
    );
  }
  return (
    mediaText(output.video_id)
      ? videoDownloadUrl(String(output.video_id))
      : mediaText(output.url)
  );
}

export function canvasOutputDownloadFilename(
  output: CanvasOutput,
  title?: string,
): string {
  const source = canvasOutputDownloadSource(output);
  const rawStem =
    mediaText(output.label) ??
    mediaText(title) ??
    fallbackDownloadStem(output);
  const extension =
    sourceExtension(rawStem) ??
    sourceExtension(mediaText(output.url)) ??
    sourceExtension(source) ??
    (output.type === "video" ? ".mp4" : ".png");
  const stem = sanitizeDownloadStem(stripKnownExtension(rawStem));
  return `${stem || fallbackDownloadStem(output)}${extension}`;
}

async function triggerCanvasOutputDownload(
  output: CanvasOutput,
  title?: string,
): Promise<"downloaded" | "opened"> {
  const source = canvasOutputDownloadSource(output);
  if (!source) throw new Error("canvas_output_download_unavailable");
  const filename = canvasOutputDownloadFilename(output, title);

  if (output.type === "image") {
    try {
      await triggerImageDownload(source, filename);
      return "downloaded";
    } catch (error) {
      if (openOriginalSource(source)) return "opened";
      throw error;
    }
  }

  triggerAnchorDownload(source, filename);
  return output.video_id || isSameOrigin(source) ? "downloaded" : "opened";
}

function triggerAnchorDownload(source: string, filename: string) {
  if (typeof document === "undefined") {
    throw new Error("canvas_output_download_unavailable");
  }
  const anchor = document.createElement("a");
  anchor.href = source;
  anchor.download = filename;
  anchor.rel = "noopener noreferrer";
  if (!isSameOrigin(source)) anchor.target = "_blank";
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

function openOriginalSource(source: string): boolean {
  if (typeof window === "undefined") return false;
  return Boolean(window.open(source, "_blank", "noopener,noreferrer"));
}

function isSameOrigin(source: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    return new URL(source, window.location.href).origin === window.location.origin;
  } catch {
    return false;
  }
}

function fallbackDownloadStem(output: CanvasOutput): string {
  const id =
    mediaText(output.image_id) ??
    mediaText(output.video_id) ??
    mediaText(output.generation_id) ??
    mediaText(output.video_generation_id);
  const prefix =
    output.type === "video" ? "lumen-canvas-video" : "lumen-canvas-image";
  return id ? `${prefix}-${id.slice(0, 12)}` : prefix;
}

function mediaText(value: string | null | undefined): string | null {
  const text = value?.trim();
  return text ? text : null;
}

function sanitizeDownloadStem(value: string): string {
  return value
    .replace(/[\u0000-\u001f\u007f/\\?%*:|"<>]/g, "-")
    .replace(/\s+/g, " ")
    .replace(/[.\s]+$/g, "")
    .trim()
    .slice(0, 96);
}

function stripKnownExtension(value: string): string {
  return value.replace(
    /\.(?:png|jpe?g|webp|gif|avif|mp4|webm|mov|m4v)$/i,
    "",
  );
}

function sourceExtension(source: string | null): string | null {
  if (!source) return null;
  const dataMime = source.match(
    /^data:(image\/(?:png|jpeg|webp|gif|avif)|video\/(?:mp4|webm|quicktime))/i,
  )?.[1];
  if (dataMime) return mimeExtension(dataMime);
  try {
    const pathname = new URL(source, "http://localhost").pathname;
    return (
      pathname.match(/\.(png|jpe?g|webp|gif|avif|mp4|webm|mov|m4v)$/i)?.[0]
        .toLowerCase() ?? null
    );
  } catch {
    return null;
  }
}

function mimeExtension(mime: string): string {
  return (
    {
      "image/png": ".png",
      "image/jpeg": ".jpg",
      "image/webp": ".webp",
      "image/gif": ".gif",
      "image/avif": ".avif",
      "video/mp4": ".mp4",
      "video/webm": ".webm",
      "video/quicktime": ".mov",
    }[mime.toLowerCase()] ?? ""
  );
}
