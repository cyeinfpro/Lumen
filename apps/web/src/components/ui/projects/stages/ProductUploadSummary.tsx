"use client";

// 上传商品后的只读摘要（current_step=upload_product 时显示，但实际很少在该阶段停留）。
// Editorial：仅 hairline + ImageGrid，无嵌套卡片。

import { useState } from "react";

import type { BackendImageMeta, WorkflowRun } from "@/lib/apiClient";
import { ImageGrid } from "../components/ImageGrid";
import { ImagePreviewModal } from "../components/ImagePreviewModal";
import { StageFrame } from "../components/StageFrame";

export function ProductUploadSummary({ workflow }: { workflow: WorkflowRun }) {
  const [previewIndex, setPreviewIndex] = useState(-1);
  const images: BackendImageMeta[] = workflow.product_images;
  return (
    <StageFrame
      eyebrow="N°01 — Product Upload"
      title="上传商品"
      subtitle="商品图已绑定到项目，后续阶段可恢复使用。"
    >
      <div className="border-t border-[var(--border)] py-4">
        <ImageGrid
          images={images}
          onPreview={(_image, index) => setPreviewIndex(index)}
        />
      </div>
      <ImagePreviewModal
        images={images}
        index={previewIndex}
        onIndexChange={setPreviewIndex}
        onClose={() => setPreviewIndex(-1)}
      />
    </StageFrame>
  );
}
