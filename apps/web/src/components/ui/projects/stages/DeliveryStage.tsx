"use client";

// 交付阶段（editorial 重构）：
// 1) "下载全部"按钮（依次触发各图下载，避免浏览器并发拦截）
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
      eyebrow="N°08 — Delivery"
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
      <section className="flex flex-wrap items-center gap-3 border-t border-[var(--border)] py-4">
        <Button
          variant="outline"
          size="sm"
          loading={reopen.isPending}
          onClick={() => setConfirmReopen(true)}
          leftIcon={<RefreshCw className="h-3.5 w-3.5" />}
        >
          重选模特
        </Button>
      </section>

      <section className="border-t border-[var(--border)] py-5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            Final Showcases
          </p>
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)] tabular-nums">
            {String(images.length).padStart(2, "0")} shots
          </p>
        </div>
        {images.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-3 border border-dashed border-[var(--border)] py-12 text-center">
            <ArchiveRestore className="h-5 w-5 text-[var(--fg-3)]" />
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
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
                  className="relative block aspect-[4/5] w-full overflow-hidden bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60"
                >
                  <Image
                    src={imageSrc(image)}
                    alt="最终展示图"
                    fill
                    sizes="(max-width: 768px) 50vw, 360px"
                    unoptimized
                    className="h-full w-full object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.04]"
                  />
                  <span className="pointer-events-none absolute left-3 top-3 font-mono text-[10px] uppercase tracking-[0.2em] text-white/90 mix-blend-difference">
                    N°{String(index + 1).padStart(2, "0")}
                  </span>
                </button>
                <a
                  href={canDownload(image) || "#"}
                  download
                  rel="noopener"
                  className="mt-2 inline-flex h-10 w-full items-center justify-center gap-1.5 border-b border-[var(--border)] font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-strong)] hover:text-[var(--fg-0)]"
                >
                  <Download className="h-3 w-3" />
                  Download
                </a>
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
          reopen.mutate();
          setConfirmReopen(false);
        }}
      />
    </StageFrame>
  );
}
