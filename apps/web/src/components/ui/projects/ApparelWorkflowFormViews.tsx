import type { DragEvent, ReactNode, RefObject } from "react";
import {
  ArrowDown,
  ArrowRight,
  ArrowUp,
  Loader2,
  RotateCcw,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/primitives/Button";
import { MediaControlButton } from "@/components/ui/primitives/MediaControlButton";
import { Select } from "@/components/ui/primitives/Select";
import { cn } from "@/lib/utils";
import { MAX_PRODUCT_IMAGES, MAX_PRODUCT_IMAGE_BYTES } from "./types";
import { formatBytes } from "./utils";

const ACCEPT = ["image/png", "image/jpeg", "image/webp"];

interface PendingFile {
  uid: string;
  file: File;
  url: string;
  progress: number;
  status: "queued" | "uploading" | "done" | "error" | "canceled";
  error?: string;
  uploadedId?: string;
  controller?: AbortController;
}

export function ApparelProductImagesSection({
  files,
  fileInputRef,
  isBusy,
  dragActive,
  anyUploading,
  totalProgress,
  onDragActiveChange,
  onDrop,
  onPickFiles,
  onUploadOne,
  onMoveFile,
  onRemoveFile,
}: {
  files: PendingFile[];
  fileInputRef: RefObject<HTMLInputElement | null>;
  isBusy: boolean;
  dragActive: boolean;
  anyUploading: boolean;
  totalProgress: number;
  onDragActiveChange: (active: boolean) => void;
  onDrop: (event: DragEvent) => void;
  onPickFiles: (list: FileList | null) => void;
  onUploadOne: (file: PendingFile) => Promise<string | null>;
  onMoveFile: (uid: string, direction: -1 | 1) => void;
  onRemoveFile: (uid: string) => void;
}) {
  return (
    <>
      <SectionHeader
        eyebrow="N°01 — 上传"
        title="商品图"
        trailing={
          <span className="type-caption tabular-nums text-[var(--fg-2)]">
            {String(files.length).padStart(2, "0")} /{" "}
            {String(MAX_PRODUCT_IMAGES).padStart(2, "0")}
          </span>
        }
      />
      <div
        onDragEnter={(event) => {
          event.preventDefault();
          onDragActiveChange(true);
        }}
        onDragOver={(event) => {
          event.preventDefault();
          if (!dragActive) onDragActiveChange(true);
        }}
        onDragLeave={(event) => {
          if (event.currentTarget.contains(event.relatedTarget as Node)) return;
          onDragActiveChange(false);
        }}
        onDrop={onDrop}
        className="relative -mt-3 md:-mt-4"
      >
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={isBusy}
          className={cn(
            "flex min-h-[188px] w-full cursor-pointer flex-col items-center justify-center gap-3 border border-dashed px-3 text-center transition-[background-color,border-color] duration-[var(--dur-base)] focus-visible:outline-none focus-visible:ring-2 focus-visible:shadow-[var(--ring)] disabled:cursor-not-allowed disabled:opacity-60 sm:min-h-[220px] md:min-h-[260px]",
            dragActive
              ? "border-accent-border bg-[var(--accent-soft)]"
              : "border-[var(--border-strong)] hover:border-accent-border hover:bg-[var(--bg-2)]",
          )}
        >
          <span
            className={cn(
              "inline-flex h-11 w-11 items-center justify-center rounded-full border transition-colors md:h-12 md:w-12",
              dragActive
                ? "border-accent-border bg-[var(--accent)] text-[var(--accent-on)]"
                : "border-[var(--border)] bg-transparent text-[var(--fg-1)]",
            )}
          >
            <Upload className="h-5 w-5" strokeWidth={1.5} />
          </span>
          <p
            className={cn(
              "type-section-title ",
              dragActive
                ? "text-accent"
                : "text-[var(--fg-0)]",
            )}
          >
            {dragActive ? "松开即可加入项目" : "拖拽到这里，或点击选择"}
          </p>
          <p className="max-w-[18rem] type-caption leading-5 text-[var(--fg-2)] ">
            PNG / JPG / WebP 格式 &nbsp;·&nbsp; ≤{" "}
            {formatBytes(MAX_PRODUCT_IMAGE_BYTES)} &nbsp;·&nbsp; 最多{" "}
            {MAX_PRODUCT_IMAGES} 张
          </p>
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPT.join(",")}
          multiple
          className="hidden"
          onChange={(event) => {
            onPickFiles(event.target.files);
            event.target.value = "";
          }}
        />
      </div>
      {anyUploading ? (
        <div className="-mt-6 grid gap-2">
          <div className="flex items-center justify-between type-caption text-[var(--fg-2)]">
            <span>上传中</span>
            <span className="tabular-nums text-accent">
              {Math.round(totalProgress * 100).toString().padStart(2, "0")}%
            </span>
          </div>
          <div className="relative h-px w-full bg-[var(--border)]">
            <div
              className="absolute inset-y-0 left-0 bg-accent transition-[width] duration-200 ease-out"
              style={{ width: `${totalProgress * 100}%` }}
            />
          </div>
        </div>
      ) : null}
      {files.length > 0 ? (
        <ul className="-mt-4 grid grid-cols-1 gap-x-4 gap-y-8 min-[390px]:grid-cols-2 md:grid-cols-3 md:gap-x-6">
          {files.map((item, index) => (
            <FilePortrait
              key={item.uid}
              item={item}
              index={index}
              total={files.length}
              locked={isBusy}
              onRetry={() => onUploadOne(item)}
              onCancel={() => item.controller?.abort()}
              onMoveUp={() => onMoveFile(item.uid, -1)}
              onMoveDown={() => onMoveFile(item.uid, 1)}
              onRemove={() => onRemoveFile(item.uid)}
            />
          ))}
        </ul>
      ) : null}
    </>
  );
}

export function ApparelCreateButton({
  isBusy,
  allDone,
  disabled,
  mobile = false,
  onClick,
}: {
  isBusy: boolean;
  allDone: boolean;
  disabled: boolean;
  mobile?: boolean;
  onClick: () => void;
}) {
  const wrapperClass = mobile
    ? "fixed inset-x-0 bottom-[var(--mobile-tabbar-height)] z-[var(--z-composer)] border-t border-[var(--border)] bg-[var(--bg-0)]/95 px-3 py-3 backdrop-blur-xl min-[390px]:px-4 md:hidden"
    : "hidden border-t border-[var(--border)] pt-6 md:block";
  return (
    <div className={wrapperClass}>
      <Button
        variant="primary"
        size="md"
        fullWidth={mobile}
        onClick={onClick}
        disabled={disabled}
        loading={isBusy}
        rightIcon={!mobile && !isBusy ? <ArrowRight className="h-4 w-4" /> : undefined}
        className={cn(!mobile && "group px-6")}
      >
        <span>{apparelCreateLabel(isBusy, allDone)}</span>
      </Button>
    </div>
  );
}

export function apparelCreateLabel(isBusy: boolean, allDone: boolean): string {
  if (isBusy) return "项目创建中";
  return allDone ? "创建分析" : "上传并创建";
}

// hairline section header：mono eyebrow + compact title + 可选右侧元素
export function SectionHeader({
  eyebrow,
  title,
  trailing,
}: {
  eyebrow: string;
  title: string;
  trailing?: ReactNode;
}) {
  return (
    <header className="border-t border-[var(--border)] pt-5">
      <div className="flex items-end justify-between gap-4">
        <div className="min-w-0">
          <p className="type-page-kicker">
            {eyebrow}
          </p>
          <h2 className="type-section-title mt-2 ">
            {title}
          </h2>
        </div>
        {trailing ? <div className="shrink-0 self-end pb-1.5">{trailing}</div> : null}
      </div>
    </header>
  );
}

export function CharCount({ remaining, max }: { remaining: number; max: number }) {
  const usage = (max - remaining) / max;
  const warning = usage > 0.92;
  return (
    <span
      className={cn(
        "type-caption tabular-nums",
        warning ? "text-[var(--warning)]" : "text-[var(--fg-2)]",
      )}
    >
      {Math.max(0, remaining)} / {max}
    </span>
  );
}

// Portrait 商品图卡：4/5 大图 + N° 序号 + 控件 + mono 元数据
export function FilePortrait({
  item,
  index,
  total,
  locked,
  onRetry,
  onCancel,
  onMoveUp,
  onMoveDown,
  onRemove,
}: {
  item: PendingFile;
  index: number;
  total: number;
  locked: boolean;
  onRetry: () => void;
  onCancel: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
}) {
  const isMain = index === 0;
  const num = `N°${String(index + 1).padStart(2, "0")}`;
  const presentation = pendingFilePresentation(item.status);

  return (
    <li className="group relative">
      <div
        className={cn(
          "relative aspect-[4/5] overflow-hidden border bg-[var(--bg-2)] transition-colors duration-[var(--dur-base)]",
          presentation.borderClass,
        )}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={item.url}
          alt={item.file.name}
          className="h-full w-full object-cover"
        />

        {/* gradient for legibility */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 h-20 bg-gradient-to-b from-[var(--media-control-bg)] to-transparent"
        />

        {/* uploading overlay */}
        {item.status === "uploading" ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-[var(--media-control-bg)] backdrop-blur-sm">
            <Loader2 className="h-5 w-5 animate-spin text-[var(--media-control-fg)]" />
            <p className="type-caption tabular-nums text-[var(--media-control-fg)]">
              {Math.round(item.progress * 100).toString().padStart(2, "0")}%
            </p>
          </div>
        ) : null}

        {/* error overlay */}
        {item.status === "error" ? (
          <div
            role="alert"
            className="absolute inset-x-0 bottom-0 bg-danger px-3 py-2"
          >
            <p className="type-caption text-[var(--danger-on)]">
              失败
            </p>
            <p className="type-caption mt-1 line-clamp-2 text-[var(--danger-on)]">
              {item.error}
            </p>
          </div>
        ) : null}

        {/* top-left N° + main chip */}
        <div className="absolute left-3 top-3 flex items-center gap-2">
          <span className="type-caption rounded-[var(--radius-control)] bg-[var(--media-control-bg)] px-2 py-1 text-[var(--media-control-fg)]">
            {num}
          </span>
          {isMain ? (
            <span className="type-caption rounded-[var(--radius-control)] bg-[var(--accent)] px-2 py-1 text-[var(--accent-on)]">
              主图
            </span>
          ) : null}
        </div>

        {/* top-right controls */}
        <FilePortraitControls
          status={item.status}
          index={index}
          total={total}
          locked={locked}
          onRetry={onRetry}
          onCancel={onCancel}
          onMoveUp={onMoveUp}
          onMoveDown={onMoveDown}
          onRemove={onRemove}
        />
      </div>

      {/* meta row */}
      <div className="mt-3 flex min-w-0 items-baseline justify-between gap-3 type-caption text-[var(--fg-2)]">
        <span className={cn("truncate", presentation.textClass)} title={item.file.name}>
          {presentation.label}
        </span>
        <span className="shrink-0 tabular-nums">{formatBytes(item.file.size)}</span>
      </div>
      <p
        className="mt-1 truncate type-caption text-[var(--fg-1)]"
        title={item.file.name}
      >
        {item.file.name}
      </p>
    </li>
  );
}

export function pendingFilePresentation(status: PendingFile["status"]) {
  switch (status) {
    case "uploading":
      return {
        label: "上传中",
        borderClass: "border-[var(--border)]",
        textClass: "text-accent",
      };
    case "done":
      return {
        label: "已就绪",
        borderClass: "border-[var(--border)]",
        textClass: "text-[var(--success)]",
      };
    case "error":
      return {
        label: "失败",
        borderClass: "border-danger-border",
        textClass: "text-[var(--danger)]",
      };
    case "canceled":
      return {
        label: "已取消",
        borderClass: "border-[var(--border)]",
        textClass: "text-[var(--fg-2)]",
      };
    default:
      return {
        label: "排队中",
        borderClass: "border-[var(--border)]",
        textClass: "text-[var(--fg-2)]",
      };
  }
}

export function FilePortraitControls({
  status,
  index,
  total,
  locked,
  onRetry,
  onCancel,
  onMoveUp,
  onMoveDown,
  onRemove,
}: {
  status: PendingFile["status"];
  index: number;
  total: number;
  locked: boolean;
  onRetry: () => void;
  onCancel: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  onRemove: () => void;
}) {
  return (
    <div className="absolute inset-x-2 top-2 flex justify-end opacity-100 transition-opacity duration-[var(--dur-base)] group-hover:opacity-100 focus-within:opacity-100 md:inset-x-auto md:right-2 md:opacity-0">
      <div className="flex gap-0.5 rounded-full border border-[var(--border-strong)] bg-[var(--media-control-bg)] p-0.5 backdrop-blur md:flex-col md:gap-1 md:p-1">
        <IconBtn label="上移" onClick={onMoveUp} disabled={locked || index === 0}>
          <ArrowUp className="h-3.5 w-3.5" />
        </IconBtn>
        <IconBtn label="下移" onClick={onMoveDown} disabled={locked || index === total - 1}>
          <ArrowDown className="h-3.5 w-3.5" />
        </IconBtn>
        {status === "error" ? (
          <span aria-live="polite" className="contents">
            <IconBtn label="重试" onClick={onRetry} disabled={locked}>
              <RotateCcw className="h-3.5 w-3.5" />
            </IconBtn>
          </span>
        ) : null}
        {status === "uploading" ? (
          <IconBtn label="取消" onClick={onCancel}>
            <X className="h-3.5 w-3.5" />
          </IconBtn>
        ) : null}
        <IconBtn label="移除" onClick={onRemove} disabled={locked} danger>
          <Trash2 className="h-3.5 w-3.5" />
        </IconBtn>
      </div>
    </div>
  );
}

export function IconBtn({
  label,
  onClick,
  disabled,
  danger,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
  children: ReactNode;
}) {
  return (
    <MediaControlButton
      size="sm"
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className={danger ? "hover:bg-danger hover:text-[var(--danger-on)]" : undefined}
    >
      {children}
    </MediaControlButton>
  );
}

export function ParamSelect({
  label,
  chineseLabel,
  value,
  options,
  onChange,
}: {
  label: string;
  chineseLabel: string;
  value: string;
  options: readonly (readonly [string, string])[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="block min-w-0">
      <span className="type-label text-[var(--fg-1)]">
        {chineseLabel || label}
      </span>
      <Select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        wrapperClassName="mt-2"
      >
        {options.map(([text, optionValue]) => (
          <option key={`${label}-${text}`} value={optionValue}>
            {text}
          </option>
        ))}
      </Select>
    </label>
  );
}
