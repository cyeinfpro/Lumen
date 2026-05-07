"use client";

// 局部修改 (inpaint) 的状态机：
//   - 仅在 image 模式 + 单张参考图 时可用（多张/无图时按钮 disabled）
//   - 打开 → MaskCanvas 弹窗 → 用户涂抹 → 确认（导出 PNG Blob → uploadImage → 拿到 image_id → setMask）
//   - mask 上传失败 fallback：保持弹窗打开（用户可重试）+ 顶部 toast
//
// 返回值由 Desktop / MobileComposerPill 共享：UI 渲染按钮 + 提示，调用 open / cancel / submit。

import { useCallback, useMemo, useState } from "react";

import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { uploadImage as apiUploadImage } from "@/lib/apiClient";
import { logError } from "@/lib/logger";
import { useChatStore } from "@/store/useChatStore";

import type { MaskExport } from "../MaskCanvas";

export type InpaintDisableReason =
  | null
  | "chat-mode"
  | "no-attachment"
  | "multi-attachment";

export function useMaskInpaint() {
  const attachments = useChatStore((s) => s.composer.attachments);
  const mode = useChatStore((s) => s.composer.mode);
  const mask = useChatStore((s) => s.composer.mask);
  const setMask = useChatStore((s) => s.setMask);
  const clearMask = useChatStore((s) => s.clearMask);

  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const disableReason: InpaintDisableReason = useMemo(() => {
    if (mode !== "image") return "chat-mode";
    if (attachments.length === 0) return "no-attachment";
    if (attachments.length > 1) return "multi-attachment";
    return null;
  }, [mode, attachments.length]);

  const disabled = disableReason !== null;

  const tooltip = useMemo(() => {
    switch (disableReason) {
      case "chat-mode":
        return "切到生图模式可用";
      case "no-attachment":
        return "先上传一张参考图";
      case "multi-attachment":
        return "局部修改仅支持单张参考图";
      default:
        return "局部修改";
    }
  }, [disableReason]);

  const openInpaint = useCallback(() => {
    if (disabled) {
      if (disableReason) pushMobileToast(tooltip, "info");
      return;
    }
    setOpen(true);
  }, [disabled, disableReason, tooltip]);

  const closeInpaint = useCallback(() => {
    if (submitting) return;
    setOpen(false);
  }, [submitting]);

  const handleConfirm = useCallback(
    async (m: MaskExport) => {
      // 防御：取最新 store 状态，避免在 dialog 打开期间被改了 attachments
      const latest = useChatStore.getState().composer;
      const target = latest.attachments[0];
      if (!target || latest.attachments.length !== 1) {
        pushMobileToast("局部修改仅支持单张参考图", "danger");
        setOpen(false);
        return;
      }
      setSubmitting(true);
      try {
        // 把 mask 当成普通图片上传（mask 是 PNG，service 已支持）
        const file = new File([m.blob], "mask.png", { type: "image/png" });
        const uploaded = await apiUploadImage(file);
        setMask({
          image_id: uploaded.id,
          preview_data_url: m.preview_data_url,
          target_attachment_id: target.id,
        });
        pushMobileToast("已设置局部修改 mask", "success");
        setOpen(false);
      } catch (err) {
        logError(err, { scope: "composer", code: "mask_upload_failed" });
        const msg = err instanceof Error ? err.message : "mask 上传失败";
        pushMobileToast(msg, "danger");
      } finally {
        setSubmitting(false);
      }
    },
    [setMask],
  );

  // 用于"已设置 mask"芯片的尺寸计算 / src 来源
  const previewSrc = mask?.preview_data_url ?? null;

  // 只渲染 mask 与第一张参考图实际匹配的状态：避免 attachments 异步变更后视觉残影
  const maskActive =
    mask !== null &&
    attachments.length === 1 &&
    attachments[0]?.id === mask.target_attachment_id;

  // 第一张参考图 src，作为 MaskCanvas 的 imageSrc
  const sourceImageSrc = attachments[0]?.data_url ?? "";

  return {
    open,
    openInpaint,
    closeInpaint,
    submitting,
    disabled,
    tooltip,
    handleConfirm,
    sourceImageSrc,
    maskActive,
    previewSrc,
    clearMask,
  };
}
