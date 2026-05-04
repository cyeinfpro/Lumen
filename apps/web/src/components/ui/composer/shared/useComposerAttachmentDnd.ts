"use client";

// 桌面/移动 ComposerPill 共用的「附件上传 + 全局拖拽」逻辑。
// 之前在 DesktopComposerPill / MobileComposerPill 各拷贝一份（~180 行/文件），
// 任何分歧都得双改。抽到 hook 后两端只剩 UI 差异。

import {
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type MutableRefObject,
  type RefObject,
  useCallback,
  useEffect,
} from "react";

import { pushMobileToast } from "@/components/ui/primitives/mobile";
import { useChatStore } from "@/store/useChatStore";

import {
  MAX_COMPOSER_ATTACHMENTS,
  hasImageFile,
  imageFilesFromDataTransfer,
  imageFilesFromList,
  remainingAttachmentSlots,
} from "./attachments";

interface UseComposerAttachmentDndOptions {
  fileInputRef: RefObject<HTMLInputElement | null>;
  dragDepthRef: MutableRefObject<number>;
  setIsUploading: (value: boolean) => void;
  setIsDragActive: (value: boolean) => void;
  setExpanded: (value: boolean) => void;
}

export function useComposerAttachmentDnd({
  fileInputRef,
  dragDepthRef,
  setIsUploading,
  setIsDragActive,
  setExpanded,
}: UseComposerAttachmentDndOptions) {
  const addAttachment = useChatStore((s) => s.addAttachment);
  const uploadAttachment = useChatStore((s) => s.uploadAttachment);
  const setComposerError = useChatStore((s) => s.setComposerError);

  const ingestFile = useCallback(
    async (file: File): Promise<boolean> => {
      if (!file.type.startsWith("image/")) return false;
      try {
        setIsUploading(true);
        const att = await uploadAttachment(file);
        addAttachment(att);
        return true;
      } catch (err) {
        const msg = err instanceof Error ? err.message : "上传失败";
        setComposerError(msg);
        pushMobileToast(msg, "danger");
        return false;
      } finally {
        setIsUploading(false);
      }
    },
    [uploadAttachment, addAttachment, setComposerError, setIsUploading],
  );

  const ingestMany = useCallback(
    async (files: File[]) => {
      const imageFiles = imageFilesFromList(files);
      if (imageFiles.length === 0) return;
      const slots = remainingAttachmentSlots(
        useChatStore.getState().composer.attachments,
      );
      if (slots <= 0) {
        const msg = `最多添加 ${MAX_COMPOSER_ATTACHMENTS} 张参考图`;
        setComposerError(msg);
        pushMobileToast(msg, "danger");
        return;
      }
      const selected = imageFiles.slice(0, slots);
      if (imageFiles.length > slots) {
        const msg = `最多添加 ${MAX_COMPOSER_ATTACHMENTS} 张参考图，已添加前 ${slots} 张`;
        setComposerError(msg);
        pushMobileToast(msg, "danger");
      }
      let ok = 0;
      for (const f of selected) {
        if (await ingestFile(f)) ok += 1;
      }
      if (ok > 0) pushMobileToast(`已添加 ${ok} 张参考图`, "success");
    },
    [ingestFile, setComposerError],
  );

  const handlePaste = useCallback(
    async (e: ClipboardEvent<HTMLTextAreaElement>) => {
      const files = imageFilesFromDataTransfer(e.clipboardData);
      if (files.length > 0) {
        e.preventDefault();
        await ingestMany(files);
      }
    },
    [ingestMany],
  );

  const handleFileInput = useCallback(
    async (e: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(e.target.files ?? []);
      await ingestMany(files);
      e.target.value = "";
    },
    [ingestMany],
  );

  const openFilePicker = useCallback(() => {
    if (
      remainingAttachmentSlots(useChatStore.getState().composer.attachments) <= 0
    ) {
      const msg = `最多添加 ${MAX_COMPOSER_ATTACHMENTS} 张参考图`;
      setComposerError(msg);
      pushMobileToast(msg, "danger");
      return;
    }
    fileInputRef.current?.click();
  }, [fileInputRef, setComposerError]);

  const handleDragEnter = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      if (!hasImageFile(e.dataTransfer)) return;
      e.preventDefault();
      e.stopPropagation();
      dragDepthRef.current += 1;
      setIsDragActive(true);
      setExpanded(true);
    },
    [dragDepthRef, setIsDragActive, setExpanded],
  );

  const handleDragOver = useCallback((e: DragEvent<HTMLDivElement>) => {
    if (!hasImageFile(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = "copy";
  }, []);

  const handleDragLeave = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      if (!hasImageFile(e.dataTransfer)) return;
      e.preventDefault();
      e.stopPropagation();
      dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
      if (dragDepthRef.current === 0) setIsDragActive(false);
    },
    [dragDepthRef, setIsDragActive],
  );

  const handleDrop = useCallback(
    async (e: DragEvent<HTMLDivElement>) => {
      if (!hasImageFile(e.dataTransfer)) return;
      e.preventDefault();
      e.stopPropagation();
      dragDepthRef.current = 0;
      setIsDragActive(false);
      const files = imageFilesFromDataTransfer(e.dataTransfer);
      await ingestMany(files);
    },
    [dragDepthRef, ingestMany, setIsDragActive],
  );

  useEffect(() => {
    const resetDragState = () => {
      dragDepthRef.current = 0;
      setIsDragActive(false);
    };

    const onDragOver = (event: globalThis.DragEvent) => {
      if (!hasImageFile(event.dataTransfer)) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      setExpanded(true);
      setIsDragActive(true);
    };

    const onDrop = (event: globalThis.DragEvent) => {
      if (!hasImageFile(event.dataTransfer)) return;
      event.preventDefault();
      resetDragState();
      const files = imageFilesFromDataTransfer(event.dataTransfer);
      void ingestMany(files);
    };

    const onDragLeave = (event: globalThis.DragEvent) => {
      const leftWindow =
        event.clientX <= 0 ||
        event.clientY <= 0 ||
        event.clientX >= window.innerWidth ||
        event.clientY >= window.innerHeight;
      if (leftWindow) resetDragState();
    };

    window.addEventListener("dragover", onDragOver);
    window.addEventListener("drop", onDrop);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("dragend", resetDragState);
    return () => {
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("drop", onDrop);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("dragend", resetDragState);
    };
  }, [dragDepthRef, ingestMany, setExpanded, setIsDragActive]);

  return {
    ingestFile,
    ingestMany,
    handlePaste,
    handleFileInput,
    openFilePicker,
    handleDragEnter,
    handleDragOver,
    handleDragLeave,
    handleDrop,
  };
}
