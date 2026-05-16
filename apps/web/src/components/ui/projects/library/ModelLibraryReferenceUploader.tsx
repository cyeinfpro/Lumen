"use client";

import { ImagePlus, Loader2, Trash2, Upload } from "lucide-react";
import Image from "next/image";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import { uploadImage } from "@/lib/apiClient";
import { cn } from "@/lib/utils";

export interface ModelLibraryReferenceValue {
  imageId: string;
  previewUrl: string;
}

interface ModelLibraryReferenceUploaderProps {
  value: ModelLibraryReferenceValue | null;
  onChange: (value: ModelLibraryReferenceValue | null) => void;
  onBusyChange?: (busy: boolean) => void;
  disabled?: boolean;
}

const MAX_REFERENCE_BYTES = 10 * 1024 * 1024;
const ACCEPTED_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);

export function ModelLibraryReferenceUploader({
  value,
  onChange,
  onBusyChange,
  disabled = false,
}: ModelLibraryReferenceUploaderProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);

  const openPicker = () => {
    if (disabled || uploading) return;
    inputRef.current?.click();
  };

  const pickFile = async (file: File | null) => {
    if (!file) return;
    if (!ACCEPTED_TYPES.has(file.type)) {
      toast.error("格式不支持", {
        description: "请上传 PNG、JPG 或 WebP 图片",
      });
      return;
    }
    if (file.size > MAX_REFERENCE_BYTES) {
      toast.error("参考图过大", {
        description: "请上传 10MB 以内的图片",
      });
      return;
    }
    setUploading(true);
    onBusyChange?.(true);
    try {
      const uploaded = await uploadImage(file);
      onChange({
        imageId: uploaded.id,
        previewUrl: uploaded.preview_url || uploaded.display_url || uploaded.url,
      });
    } catch (err) {
      toast.error("上传失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      });
    } finally {
      setUploading(false);
      onBusyChange?.(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  return (
    <div className="grid gap-2">
      <input
        ref={inputRef}
        type="file"
        accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"
        className="hidden"
        disabled={disabled || uploading}
        onChange={(event) => void pickFile(event.target.files?.[0] ?? null)}
      />
      <div
        className={cn(
          "grid gap-3 border border-[var(--border)] bg-[var(--bg-1)] p-3 shadow-[var(--shadow-1)]",
          disabled && "opacity-60",
        )}
      >
        <button
          type="button"
          onClick={openPicker}
          disabled={disabled || uploading}
          className={cn(
            "relative aspect-[4/5] w-full overflow-hidden bg-[var(--bg-2)] text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
            value ? "cursor-pointer" : "cursor-pointer border border-dashed border-[var(--border)]",
          )}
          aria-label={value ? "替换参考图" : "上传参考图"}
        >
          {uploading ? (
            <span className="flex h-full w-full items-center justify-center gap-2 font-mono text-[11px] uppercase tracking-[0.16em] text-[var(--fg-2)]">
              <Loader2 className="h-4 w-4 animate-spin" />
              上传中
            </span>
          ) : value ? (
            <Image
              src={value.previewUrl}
              alt="参考图"
              fill
              unoptimized
              sizes="(max-width: 768px) 100vw, 420px"
              className="object-cover"
            />
          ) : (
            <span className="flex h-full w-full flex-col items-center justify-center gap-2 px-4 text-center">
              <ImagePlus className="h-5 w-5 text-[var(--fg-2)]" />
              <span className="font-mono text-[10.5px] uppercase tracking-[0.16em] text-[var(--fg-1)]">
                上传参考图
              </span>
              <span className="max-w-[220px] text-[12px] leading-[1.5] text-[var(--fg-3)]">
                单人清晰人像，PNG/JPG/WebP，10MB 内
              </span>
            </span>
          )}
        </button>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-3)]">
            {value ? value.imageId : "未上传"}
          </p>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={openPicker}
              disabled={disabled || uploading}
              leftIcon={<Upload className="h-3.5 w-3.5" />}
            >
              {value ? "替换" : "上传"}
            </Button>
            {value ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onChange(null)}
                disabled={disabled || uploading}
                leftIcon={<Trash2 className="h-3.5 w-3.5" />}
              >
                清除
              </Button>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
