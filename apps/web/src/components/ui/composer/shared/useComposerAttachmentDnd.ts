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
  useRef,
} from "react";

import { pushMobileToast } from "@/components/ui/primitives/mobile";

import {
  hasImageFile,
  imageFilesFromDataTransfer,
  imageFilesFromList,
} from "./attachments";

interface UseComposerAttachmentDndOptions<TAttachment> {
  fileInputRef: RefObject<HTMLInputElement | null>;
  dragDepthRef: MutableRefObject<number>;
  setIsUploading: (value: boolean) => void;
  setIsDragActive: (value: boolean) => void;
  setExpanded: (value: boolean) => void;
  uploadAttachment: (
    file: File,
    opts: { signal: AbortSignal },
  ) => Promise<TAttachment>;
  addAttachment: (attachment: TAttachment) => void;
  getAttachmentCount: () => number;
  setError: (message: string | null) => void;
  limit: number;
  attachmentNoun?: string;
}

function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

export function useComposerAttachmentDnd<TAttachment>({
  fileInputRef,
  dragDepthRef,
  setIsUploading,
  setIsDragActive,
  setExpanded,
  uploadAttachment,
  addAttachment,
  getAttachmentCount,
  setError,
  limit,
  attachmentNoun = "参考图",
}: UseComposerAttachmentDndOptions<TAttachment>) {
  const uploadControllersRef = useRef<Set<AbortController>>(new Set());
  const limitMessage = `最多添加 ${limit} 张${attachmentNoun}`;

  const ingestFile = useCallback(
    async (file: File): Promise<boolean> => {
      if (!file.type.startsWith("image/")) return false;
      const ctl = new AbortController();
      uploadControllersRef.current.add(ctl);
      try {
        setIsUploading(true);
        const att = await uploadAttachment(file, { signal: ctl.signal });
        addAttachment(att);
        return true;
      } catch (err) {
        if (isAbortError(err)) return false;
        const msg = err instanceof Error ? err.message : "上传失败";
        setError(msg);
        pushMobileToast(msg, "danger");
        return false;
      } finally {
        uploadControllersRef.current.delete(ctl);
        setIsUploading(uploadControllersRef.current.size > 0);
        setExpanded(true);
      }
    },
    [
      uploadAttachment,
      addAttachment,
      setError,
      setExpanded,
      setIsUploading,
    ],
  );

  const ingestMany = useCallback(
    async (files: File[]) => {
      const imageFiles = imageFilesFromList(files);
      if (imageFiles.length === 0) return;
      const slots = Math.max(0, limit - getAttachmentCount());
      if (slots <= 0) {
        setError(limitMessage);
        pushMobileToast(limitMessage, "danger");
        return;
      }
      const selected = imageFiles.slice(0, slots);
      if (imageFiles.length > slots) {
        const msg = `${limitMessage}，已添加前 ${slots} 张`;
        setError(msg);
        pushMobileToast(msg, "danger");
      }
      let ok = 0;
      for (const f of selected) {
        if (await ingestFile(f)) ok += 1;
      }
      if (ok > 0) pushMobileToast(`已添加 ${ok} 张参考图`, "success");
    },
    [
      getAttachmentCount,
      ingestFile,
      limit,
      limitMessage,
      setError,
    ],
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
      getAttachmentCount() >= limit
    ) {
      setError(limitMessage);
      pushMobileToast(limitMessage, "danger");
      return;
    }
    fileInputRef.current?.click();
  }, [fileInputRef, getAttachmentCount, limit, limitMessage, setError]);

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
