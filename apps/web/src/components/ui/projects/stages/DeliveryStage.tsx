"use client";

// 交付阶段：单纯的展示 + 下载页。
// 改进：
// 1) "下载全部"按钮（依次触发各图下载，避免浏览器并发拦截）
// 2) 单图下载用 a[download]，hover 显示文件名
// 3) 重选模特 ConfirmDialog 兜底

import { ArchiveRestore, Download, RefreshCw } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { ConfirmDialog } from "@/components/ui/primitives/ConfirmDialog";
import { toast } from "@/components/ui/primitives/Toast";
import { useReopenModelSelectionMutation } from "@/lib/queries";
import type { BackendImageMeta, WorkflowRun } from "@/lib/apiClient";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { StageFrame } from "../components/StageFrame";
import { canDownload, imageSrc, showcaseImages } from "../utils";

export function DeliveryStage({ workflow }: { workflow: WorkflowRun }) {
  const reopen = useReopenModelSelectionMutation(workflow.id, {
    onError: (err) =>
      toast.error("返回重选模特失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
    onSuccess: () => toast.success("已返回模特候选阶段"),
  });
  const [previewIndex, setPreviewIndex] = useState(-1);
  const [confirmReopen, setConfirmReopen] = useState(false);
  const images = showcaseImages(workflow);

  const downloadAll = () => {
    let fired = 0;
    images.forEach((image, index) => {
      const url = canDownload(image);
      if (!url) return;
      window.setTimeout(() => {
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "";
        anchor.rel = "noopener";
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
      }, index * 220);
      fired += 1;
    });
    if (fired) toast.success(`已开始下载 ${fired} 张图`);
    else toast.warning("暂无可下载图片");
  };

  return (
    <StageFrame
      title="交付"
      subtitle="最终图已进入交付状态，可逐张或一键打包下载，也可继续返修。"
      actions={
        images.length > 0 ? (
          <Button
            variant="primary"
            onClick={downloadAll}
            leftIcon={<Download className="h-4 w-4" />}
          >
            下载全部
          </Button>
        ) : null
      }
    >
      <div className="mb-4 flex flex-wrap gap-2">
        <Button
          variant="secondary"
          loading={reopen.isPending}
          onClick={() => setConfirmReopen(true)}
          leftIcon={<RefreshCw className="h-4 w-4" />}
        >
          重选模特
        </Button>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {images.map((image: BackendImageMeta, index) => (
          <article
            key={image.id}
            className="rounded-md border border-[var(--border)] bg-white/[0.035] p-2 transition-shadow hover:shadow-[var(--shadow-2)]"
          >
            <button
              type="button"
              onClick={() => setPreviewIndex(index)}
              className="block w-full overflow-hidden rounded-md focus-visible:outline-none"
            >
              <img
                src={imageSrc(image)}
                alt="最终展示图"
                loading="lazy"
                className="aspect-[4/5] w-full object-cover transition-transform duration-[var(--dur-slow)] hover:scale-[1.02]"
              />
            </button>
            <a
              href={canDownload(image) || "#"}
              download
              rel="noopener"
              className="mt-2 inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-md border border-[var(--border)] text-sm text-[var(--fg-0)] transition-colors hover:bg-white/[0.04]"
            >
              <Download className="h-4 w-4" />
              下载
            </a>
          </article>
        ))}
        {images.length === 0 ? (
          <div className="col-span-full rounded-md border border-[var(--border)] bg-white/[0.03] p-6 text-center text-sm text-[var(--fg-2)]">
            <ArchiveRestore className="mx-auto mb-2 h-5 w-5 text-[var(--fg-3)]" />
            交付目录暂无图像
          </div>
        ) : null}
      </div>

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
          reopen.mutate();
          setConfirmReopen(false);
        }}
      />
    </StageFrame>
  );
}
