"use client";

// 海报交付阶段：下载 + 入图库 + 复制信息。
// 此阶段没有专门的"完成"接口（与 apparel showcase 用 completeWorkflowDelivery 不同），
// 后端在 multi_size_generation 完成后自动推进；前端这里只做信息汇总。

import { Check, Copy, Download } from "lucide-react";
import Image from "next/image";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import type { BackendImageMeta, PosterRender, WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { StageFrame } from "../components/StageFrame";
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

export function PosterDeliveryStage({ workflow }: { workflow: WorkflowRun }) {
  const renders = (workflow.poster_renders ?? []).filter(
    (render) => render.image_id,
  );
  const [previewList, setPreviewList] = useState<BackendImageMeta[]>([]);
  const [previewIndex, setPreviewIndex] = useState(-1);

  const downloadAll = () => {
    let count = 0;
    for (const render of renders) {
      const image = findImageById(workflow, render.image_id);
      if (!image) continue;
      const href = image.url || image.display_url;
      if (!href) continue;
      const link = document.createElement("a");
      link.href = href;
      link.download = `poster_${render.aspect_ratio}_${render.id.slice(0, 8)}.png`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      count += 1;
    }
    if (count) toast.success(`已派发 ${count} 张下载`);
    else toast.warning("没有可下载的成品");
  };

  const copySummary = async () => {
    const meta = (workflow.metadata_jsonb || {}) as Record<string, unknown>;
    const styleSummary = (meta.style_summary || {}) as Record<string, unknown>;
    const styleTitle =
      typeof styleSummary.title === "string" ? styleSummary.title : "未指定";
    const aspects = renders.map((render) => render.aspect_ratio).join(", ");
    const text =
      `项目：${workflow.title}\n` +
      `风格：${styleTitle}\n` +
      `尺寸：${aspects}\n` +
      `创建：${workflow.created_at}\n`;
    try {
      await navigator.clipboard.writeText(text);
      toast.success("项目信息已复制到剪贴板");
    } catch {
      toast.error("复制失败，请手动选择");
    }
  };

  return (
    <StageFrame
      eyebrow="N°07 — 交付"
      title="交付"
      subtitle="批量下载所有尺寸，或单张右键另存。"
      actions={
        <div className="flex flex-wrap gap-2">
          <Button
            variant="primary"
            size="sm"
            onClick={downloadAll}
            leftIcon={<Download className="h-3.5 w-3.5" />}
            disabled={!renders.length}
          >
            全部下载
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={copySummary}
            leftIcon={<Copy className="h-3.5 w-3.5" />}
          >
            复制信息
          </Button>
        </div>
      }
    >
      {!renders.length ? (
        <div className="mt-4 flex h-32 flex-col items-center justify-center gap-2 border border-dashed border-[var(--border)] text-[var(--fg-2)]">
          <span className="font-mono text-[10px] uppercase tracking-[0.18em]">
            暂无可交付的成品
          </span>
        </div>
      ) : (
        <ul className="mt-4 grid grid-cols-1 gap-x-4 gap-y-8 sm:grid-cols-2 lg:grid-cols-3">
          {renders.map((render) => (
            <DeliveryCard
              key={render.id}
              workflow={workflow}
              render={render}
              onPreview={(image) => {
                setPreviewList([image]);
                setPreviewIndex(0);
              }}
            />
          ))}
        </ul>
      )}

      <div className="mt-8 grid gap-2 border-t border-[var(--border)] pt-5">
        <p className="inline-flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--success)]">
          <Check className="h-3 w-3" />
          交付就绪
        </p>
        <p className="text-[13px] leading-[1.7] text-[var(--fg-1)]">
          也可在「图库」中找到这些成品，做后续二次编辑或分享。
        </p>
      </div>

      <ImagePreviewModal
        images={previewList}
        index={previewIndex}
        onClose={() => setPreviewIndex(-1)}
      />
    </StageFrame>
  );
}

function DeliveryCard({
  workflow,
  render,
  onPreview,
}: {
  workflow: WorkflowRun;
  render: PosterRender;
  onPreview: (image: BackendImageMeta) => void;
}) {
  const image = findImageById(workflow, render.image_id);
  if (!image) return null;
  const aspectCls = ASPECT_TO_CLASS[render.aspect_ratio] || "aspect-square";
  const downloadHref = image.url || image.display_url || "";

  return (
    <li className="group">
      <button
        type="button"
        onClick={() => onPreview(image)}
        className={cn(
          "relative block w-full overflow-hidden rounded-lg bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
          aspectCls,
        )}
      >
        <Image
          src={imageSrc(image)}
          alt={`海报 ${render.aspect_ratio}`}
          fill
          sizes="(max-width: 768px) 50vw, 320px"
          unoptimized
          className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.04]"
        />
        <span className="absolute left-3 top-3 font-mono text-[10px] uppercase tracking-[0.22em] text-white/90 mix-blend-difference">
          {render.aspect_ratio}
        </span>
      </button>

      <div className="mt-3 flex items-baseline justify-between gap-3 border-b border-[var(--border)] pb-2">
        <p className="text-[14px] font-medium tracking-tight text-[var(--fg-0)]">
          {render.aspect_ratio}
        </p>
        <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          {render.size}
        </span>
      </div>
      <div className="mt-3">
        <Button
          variant="outline"
          size="sm"
          fullWidth
          disabled={!downloadHref}
          onClick={() => {
            if (!downloadHref) return;
            const link = document.createElement("a");
            link.href = downloadHref;
            link.download = `poster_${render.aspect_ratio}_${render.id.slice(0, 8)}.png`;
            document.body.appendChild(link);
            link.click();
            link.remove();
          }}
          leftIcon={<Download className="h-3.5 w-3.5" />}
        >
          下载
        </Button>
      </div>
    </li>
  );
}
