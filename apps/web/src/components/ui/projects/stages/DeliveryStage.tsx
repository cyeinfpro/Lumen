"use client";

// 交付阶段（editorial 重构）：
// 1) 可停止、可重试的逐张下载；浏览器是否接受连续下载由用户设置决定。
// 2) 单图下载：portrait 卡 + 底部 mono underline 链接（去除嵌套圆角卡）
// 3) 重选模特 ConfirmDialog 兜底

import { ArchiveRestore, Download, RefreshCw } from "lucide-react";
import Image from "next/image";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { toast } from "@/components/ui/primitives/Toast";
import { useReopenModelSelectionMutation } from "@/lib/queries";
import type { BackendImageMeta, WorkflowRun } from "@/lib/apiClient";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { StageFrame } from "../components/StageFrame";
import {
  ProjectImageDownloadButton,
  ProjectImageDownloadStatus,
  useProjectImageDownloads,
} from "../components/ProjectImageDownloads";

import { canDownload, imageSrc, showcaseImages } from "../utils";

export function DeliveryStage({ workflow }: { workflow: WorkflowRun }) {
  const reopen = useReopenModelSelectionMutation(workflow.id, {
    onError: (err) =>
      toast.error("返回重选模特失败", {
        description: err instanceof Error ? err.message : "稍后重试",
      }),
    onSuccess: () => toast.success("已返回模特候选阶段"),
  });
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [confirmReopen, setConfirmReopen] = useState(false);
  const images = showcaseImages(workflow);

  const downloads = useProjectImageDownloads(workflow.id);
  const downloadFiles = images.flatMap((image, index) => {
    const url = canDownload(image);
    return url ? [{ url, filename: `showcase_${index + 1}_${image.id.slice(0, 8)}.png` }] : [];
  });

  return (
    <StageFrame
      eyebrow="N°08 — 交付"
      title="交付"
      subtitle="逐张下载最终展示图，或继续返修。连续下载若被浏览器拦截，可使用每张图片下方的下载按钮。"
      actions={
        images.length > 0 ? (
          <Button
            variant="primary"
            onClick={() => void downloads.start(downloadFiles)}
            disabled={!downloadFiles.length}
            loading={downloads.busy}
            leftIcon={<Download className="h-4 w-4" />}
            className="w-full sm:w-auto"
          >
            逐张下载全部
          </Button>
        ) : null
      }
    >
      <ProjectImageDownloadStatus downloads={downloads} />
      <section className="flex flex-wrap items-center gap-3 border-t border-[var(--border)] py-4">
        <Button
          variant="outline"
          size="sm"
          loading={reopen.isPending}
          disabled={downloads.busy}
          onClick={() => setConfirmReopen(true)}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
          className="w-full sm:w-auto"
        >
          重选模特
        </Button>
      </section>

      <section className="border-t border-[var(--border)] py-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <p className="type-caption text-[var(--fg-2)]">
            最终展示图
          </p>
          <p className="type-caption text-[var(--fg-3)] tabular-nums">
            {String(images.length).padStart(2, "0")} 张
          </p>
        </div>
        {images.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-3 border border-dashed border-[var(--border)] py-12 text-center">
            <ArchiveRestore className="h-5 w-5 text-[var(--fg-3)]" />
            <p className="type-caption text-[var(--fg-2)]">
              交付目录暂无图像
            </p>
          </div>
        ) : (
          <div className="grid gap-x-4 gap-y-8 md:grid-cols-2 xl:grid-cols-4">
            {images.map((image: BackendImageMeta, index) => (
              <article key={image.id} className="group relative">
                <button
                  type="button"
                  onClick={() => setPreviewIndex(index)}
                  aria-label={`预览最终展示图 ${index + 1}`}
                  className="relative block aspect-[4/5] w-full overflow-hidden bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:shadow-[var(--ring)]"
                >
                  <Image
                    src={imageSrc(image)}
                    alt="最终展示图"
                    fill
                    sizes="(max-width: 768px) 50vw, 360px"
                    unoptimized
                    className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
                  />
                  <span className="type-caption pointer-events-none absolute left-3 top-3 rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-2 py-1 text-[var(--media-control-fg)]">
                    N°{String(index + 1).padStart(2, "0")}
                  </span>
                </button>
                <div className="mt-2">
                  <ProjectImageDownloadButton
                    file={{ url: canDownload(image) ?? "", filename: `showcase_${index + 1}_${image.id.slice(0, 8)}.png` }}
                    disabled={downloads.busy}
                    label={`下载最终展示图 ${index + 1}`}
                  />
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <ImagePreviewModal
        images={images}
        index={previewIndex}
        onIndexChange={setPreviewIndex}
        onClose={() => setPreviewIndex(-1)}
      />

      <ConfirmDialog
        open={confirmReopen}
        onOpenChange={setConfirmReopen}
        title="返回重选模特？"
        description="将放弃已交付的展示图，回到模特候选阶段重新生成。"
        confirmText="返回重选"
        tone="danger"
        confirming={reopen.isPending}
        onConfirm={async () => {
          await reopen.mutateAsync();
          setConfirmReopen(false);
        }}
      />
    </StageFrame>
  );
}
