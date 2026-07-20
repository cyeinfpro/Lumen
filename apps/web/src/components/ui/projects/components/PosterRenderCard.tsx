"use client";

// 海报单尺寸渲染卡：图 + aspect + size + status + 返修 + 下载。
// 返修按钮：背景重生（scope=background）/ 局部 inpaint（scope=inpaint）。

import { Download, Loader2, Pencil, RefreshCw, Scissors } from "lucide-react";
import Image from "next/image";

import { Button } from "@/components/ui/primitives/Button";
import type { BackendImageMeta, PosterRender, WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { imageSrc } from "../utils";

function findImageById(
  workflow: WorkflowRun,
  imageId: string | null | undefined,
): BackendImageMeta | undefined {
  if (!imageId) return undefined;
  return [...workflow.product_images, ...workflow.generated_images].find(
    (image) => image.id === imageId,
  );
}

const ASPECT_TO_CLASS: Record<string, string> = {
  "1:1": "aspect-square",
  "9:16": "aspect-[9/16]",
  "16:9": "aspect-video",
  "3:4": "aspect-[3/4]",
  "4:3": "aspect-[4/3]",
  "2:3": "aspect-[2/3]",
  "3:2": "aspect-[3/2]",
  "4:5": "aspect-[4/5]",
};

function aspectClass(aspect: string): string {
  return ASPECT_TO_CLASS[aspect] || "aspect-square";
}

interface PosterRenderCardProps {
  workflow: WorkflowRun;
  render: PosterRender;
  onReviseBackground: () => void;
  onInpaint: () => void;
  onRegenerate?: () => void;
  reviseLoading?: boolean;
  onPreview?: (image: BackendImageMeta) => void;
}

function PosterPreview({
  image,
  render,
  isGenerating,
  isFailed,
  onPreview,
}: {
  image?: BackendImageMeta;
  render: PosterRender;
  isGenerating: boolean;
  isFailed: boolean;
  onPreview?: (image: BackendImageMeta) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => {
        if (image && onPreview) onPreview(image);
      }}
      disabled={!image}
      className={cn(
        "relative block w-full overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-2)] transition-shadow duration-[var(--dur-base)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
        aspectClass(render.aspect_ratio),
      )}
    >
      {image ? (
        <Image
          src={imageSrc(image)}
          alt={`海报 ${render.aspect_ratio}`}
          fill
          sizes="(max-width: 768px) 50vw, 360px"
          unoptimized
          className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
        />
      ) : (
        <div className="flex h-full flex-col items-center justify-center gap-2 text-[var(--fg-2)]">
          {isGenerating ? (
            <>
              <Loader2 className="h-5 w-5 animate-spin" />
              <span className="font-mono text-[10px] uppercase tracking-[0.18em]">
                {render.status === "revising" ? "返修中" : "生成中"}
              </span>
            </>
          ) : isFailed ? (
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--danger)]">
              生成失败
            </span>
          ) : (
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
              等待中
            </span>
          )}
        </div>
      )}

      <span className="absolute left-3 top-3 font-mono text-[10px] uppercase tracking-[0.22em] text-white/90 mix-blend-difference">
        {render.aspect_ratio}
      </span>
    </button>
  );
}

function downloadPoster(downloadHref: string, render: PosterRender) {
  if (!downloadHref) return;
  const link = document.createElement("a");
  link.href = downloadHref;
  link.download = `poster_${render.aspect_ratio}_${render.id.slice(0, 8)}.png`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

export function PosterRenderCard({
  workflow,
  render,
  onReviseBackground,
  onInpaint,
  onRegenerate,
  reviseLoading = false,
  onPreview,
}: PosterRenderCardProps) {
  const image = findImageById(workflow, render.image_id);
  const isGenerating =
    render.status === "generating" || render.status === "revising";
  const isFailed = render.status === "failed";
  const isReady = render.status === "ready" || render.status === "completed";

  const downloadHref = image?.url || image?.display_url || "";

  return (
    <li className="group relative">
      <PosterPreview
        image={image}
        render={render}
        isGenerating={isGenerating}
        isFailed={isFailed}
        onPreview={onPreview}
      />

      <div className="mt-3 flex items-baseline justify-between gap-3 border-b border-[var(--border)] pb-2">
        <p className="text-[14px] font-medium tracking-tight text-[var(--fg-0)]">
          {render.aspect_ratio} 成品
        </p>
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          {render.size}
        </span>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={!isReady || reviseLoading}
          onClick={onReviseBackground}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          背景重生
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={!isReady || reviseLoading}
          onClick={onInpaint}
          leftIcon={<Scissors className="h-3.5 w-3.5" />}
        >
          局部修复
        </Button>
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2">
        <Button
          variant="ghost"
          size="sm"
          disabled={!downloadHref}
          onClick={() => downloadPoster(downloadHref, render)}
          leftIcon={<Download className="h-3.5 w-3.5" />}
        >
          下载
        </Button>
        {onRegenerate ? (
          <Button
            variant="ghost"
            size="sm"
            disabled={!isReady || reviseLoading}
            onClick={onRegenerate}
            leftIcon={<Pencil className="h-3.5 w-3.5" />}
          >
            自定义返修
          </Button>
        ) : null}
      </div>
    </li>
  );
}
