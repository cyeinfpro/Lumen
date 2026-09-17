"use client";

// 海报交付阶段：下载 + 写入项目资产 + 复制信息。

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Download, FolderPlus } from "lucide-react";
import Image from "next/image";
import { useState } from "react";

import { useUserQueryScope } from "@/components/QueryProvider";
import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import {
  apiFetch,
  type BackendImageMeta,
  type PosterRender,
  type WorkflowRun,
} from "@/lib/apiClient";
import { qk } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { copyTextToClipboard } from "@/lib/clipboard";
import {
  ProjectImageDownloadButton,
  ProjectImageDownloadStatus,
  useProjectImageDownloads,
} from "../components/ProjectImageDownloads";

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
  const userScope = useUserQueryScope();
  const userKeys = qk.user(userScope.userId);
  const renders = (workflow.poster_renders ?? []).filter(
    (render) => render.image_id,
  );
  const renderImageIds = renders
    .map((render) => render.image_id)
    .filter((imageId): imageId is string => Boolean(imageId));
  const savedAssetIds = workflowAssetImageIds(workflow, "poster_delivery");
  const allSaved =
    renderImageIds.length > 0 && renderImageIds.every((imageId) => savedAssetIds.has(imageId));
  const [previewList, setPreviewList] = useState<BackendImageMeta[]>([]);
  const [previewIndex, setPreviewIndex] = useState(-1);
  const queryClient = useQueryClient();
  const saveAssets = useMutation<WorkflowRun, Error, string[]>({
    mutationFn: (image_ids) =>
      apiFetch<WorkflowRun>(`/workflows/${workflow.id}/assets`, {
        method: "POST",
        body: JSON.stringify({
          image_ids,
          asset_type: "poster_delivery",
          source_step_key: "delivery",
          label: "海报交付",
        }),
    }),
    onSuccess: (data) => {
      queryClient.setQueryData(userKeys.workflow(workflow.id), data);
      queryClient.invalidateQueries({ queryKey: userKeys.workflowsAll() });
      toast.success("海报成品已加入项目素材");
    },
    onError: (error) => {
      toast.error("加入项目素材失败", {
        description: error.message || "稍后重试",
      });
    },
  });

  const downloads = useProjectImageDownloads(workflow.id);
  const downloadFiles = renders.flatMap((render) => {
    const image = findImageById(workflow, render.image_id);
    const url = image?.url || image?.display_url;
    return url ? [{ url, filename: `poster_${render.aspect_ratio}_${render.id.slice(0, 8)}.png` }] : [];
  });

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
      await copyTextToClipboard(text);
      toast.success("项目信息已复制到剪贴板");
    } catch {
      toast.error("复制失败，请检查浏览器剪贴板权限后重试");
    }
  };

  return (
    <StageFrame
      eyebrow="N°07 — 交付"
      title="交付"
      subtitle="逐张下载所有尺寸，并可加入项目素材。若浏览器拦截连续下载，请使用单张下载按钮。"
      actions={
        <div className="grid grid-cols-1 gap-2 min-[420px]:grid-cols-2 sm:flex sm:flex-wrap">
          <Button
            variant="primary"
            size="sm"
            onClick={() => void downloads.start(downloadFiles)}
            loading={downloads.busy}
            leftIcon={<Download className="h-3.5 w-3.5" />}
            disabled={!downloadFiles.length}
            className="w-full sm:w-auto"
          >
            逐张下载全部
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => saveAssets.mutate(renderImageIds)}
            leftIcon={<FolderPlus className="h-3.5 w-3.5" />}
            disabled={!renderImageIds.length || allSaved}
            loading={saveAssets.isPending}
            className="w-full sm:w-auto"
          >
            {allSaved ? "已加入项目素材" : "加入项目素材"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={copySummary}
            leftIcon={<Copy className="h-3.5 w-3.5" />}
            className="w-full sm:w-auto"
          >
            复制信息
          </Button>
        </div>
      }
    >
      <ProjectImageDownloadStatus downloads={downloads} />
      {!renders.length ? (
        <div className="mt-4 flex h-32 flex-col items-center justify-center gap-2 border border-dashed border-[var(--border)] text-[var(--fg-2)]">
          <span className="type-caption">
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
              downloading={downloads.busy}
              onPreview={(image) => {
                setPreviewList([image]);
                setPreviewIndex(0);
              }}
            />
          ))}
        </ul>
      )}

      <div className="mt-8 grid gap-2 border-t border-[var(--border)] pt-5">
        <p className="inline-flex items-center gap-2 type-caption text-[var(--success)]">
          <Check className="h-3 w-3" />
          {renders.length ? "交付就绪" : "尚无可交付成品"}
        </p>
        <p className="type-body-sm leading-[1.7] text-[var(--fg-1)]">
          {allSaved
            ? "这些成品已保存为项目素材，可从项目中心继续追踪与复用。"
            : "下载前建议先加入项目素材，便于后续查找、复用和交付复盘。"}
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

function workflowAssetImageIds(workflow: WorkflowRun, assetType: string): Set<string> {
  const rawAssets = workflow.metadata_jsonb?.assets;
  const ids = new Set<string>();
  if (!Array.isArray(rawAssets)) return ids;
  for (const asset of rawAssets) {
    if (!asset || typeof asset !== "object") continue;
    const record = asset as Record<string, unknown>;
    if (record.asset_type !== assetType) continue;
    if (typeof record.image_id === "string" && record.image_id) {
      ids.add(record.image_id);
    }
  }
  return ids;
}

function DeliveryCard({
  workflow,
  render,
  onPreview,
  downloading,
}: {
  workflow: WorkflowRun;
  render: PosterRender;
  downloading: boolean;
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
        aria-label={`预览海报 ${render.aspect_ratio}`}
        className={cn(
          "relative block w-full overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:shadow-[var(--ring)]",
          aspectCls,
        )}
      >
        <Image
          src={imageSrc(image)}
          alt={`海报 ${render.aspect_ratio}`}
          fill
          sizes="(max-width: 768px) 50vw, 320px"
          unoptimized
          className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
        />
        <span className="type-caption absolute left-3 top-3 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-2 py-1 text-[var(--media-control-fg)]">
          {render.aspect_ratio}
        </span>
      </button>

      <div className="mt-3 flex items-baseline justify-between gap-3 border-b border-[var(--border)] pb-2">
        <p className="type-body-sm font-medium tracking-tight text-[var(--fg-0)]">
          {render.aspect_ratio}
        </p>
        <span className="type-caption text-[var(--fg-2)]">
          {render.size}
        </span>
      </div>
      <div className="mt-3">
        <ProjectImageDownloadButton
          file={{ url: downloadHref, filename: `poster_${render.aspect_ratio}_${render.id.slice(0, 8)}.png` }}
          disabled={downloading}
          label={`下载海报 ${render.aspect_ratio}`}
        />
      </div>
    </li>
  );
}
