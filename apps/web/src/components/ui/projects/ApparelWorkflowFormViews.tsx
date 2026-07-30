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
          <span className="font-mono text-[11px] uppercase tracking-[0.18em] tabular-nums text-[var(--fg-2)]">
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
            "flex min-h-[188px] w-full cursor-pointer flex-col items-center justify-center gap-3 border border-dashed px-3 text-center transition-[background-color,border-color] duration-[var(--dur-base)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60 disabled:cursor-not-allowed disabled:opacity-60 sm:min-h-[220px] md:min-h-[260px]",
            dragActive
              ? "border-[var(--border-amber)] bg-[var(--accent-soft)]"
              : "border-[var(--border-strong)] hover:border-[var(--border-amber)]/50 hover:bg-[var(--bg-2)]",
          )}
        >
          <span
            className={cn(
              "inline-flex h-11 w-11 items-center justify-center rounded-full border transition-colors md:h-12 md:w-12",
              dragActive
                ? "border-[var(--border-amber)] bg-[var(--accent)] text-black"
                : "border-[var(--border)] bg-transparent text-[var(--fg-1)]",
            )}
          >
            <Upload className="h-5 w-5" strokeWidth={1.5} />
          </span>
          <p
            className={cn(
              "type-section-title md:text-[20px]",
              dragActive
                ? "text-[var(--amber-300)]"
                : "text-[var(--fg-0)]",
            )}
          >
            {dragActive ? "松开即可加入项目" : "拖拽到这里，或点击选择"}
          </p>
          <p className="max-w-[18rem] font-mono text-[10px] uppercase tracking-[0.12em] leading-5 text-[var(--fg-2)] sm:tracking-[0.22em]">
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
          <div className="flex items-center justify-between font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
            <span>上传中</span>
            <span className="tabular-nums text-[var(--amber-300)]">
              {Math.round(totalProgress * 100).toString().padStart(2, "0")}%
            </span>
          </div>
          <div className="relative h-px w-full bg-[var(--border)]">
            <div
              className="absolute inset-y-0 left-0 bg-[var(--amber-400)] transition-[width] duration-200 ease-out"
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
    ? "fixed inset-x-0 bottom-[var(--mobile-tabbar-height)] z-30 border-t border-[var(--border)] bg-[var(--bg-0)]/95 px-3 py-3 backdrop-blur-xl min-[390px]:px-4 md:hidden"
    : "hidden border-t border-[var(--border)] pt-6 md:block";
  const buttonClass = mobile
    ? "inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-full px-5 py-3 text-[15px] font-medium text-black transition-[opacity,transform] duration-[var(--dur-base)]"
    : "group inline-flex items-center gap-3 rounded-full px-7 py-3.5 font-medium text-black shadow-[var(--shadow-amber)] transition-[transform,opacity,box-shadow] duration-[var(--dur-base)]";
  const enabledClass = mobile
    ? "cursor-pointer bg-[var(--accent)] shadow-[var(--shadow-amber)] active:scale-[0.98]"
    : "cursor-pointer bg-[var(--accent)] hover:scale-[1.02] active:scale-[0.98]";
  return (
    <div className={wrapperClass}>
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        className={cn(
          buttonClass,
          disabled
            ? "cursor-not-allowed bg-[var(--fg-3)] opacity-60"
            : enabledClass,
        )}
      >
        {isBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
        <span>{apparelCreateLabel(isBusy, allDone)}</span>
        {!mobile && !isBusy ? (
          <ArrowRight className="h-4 w-4 -translate-x-1 opacity-0 transition-all duration-[var(--dur-base)] group-enabled:group-hover:translate-x-0 group-enabled:group-hover:opacity-100" />
        ) : null}
      </button>
    </div>
  );
}

export function apparelCreateLabel(isBusy: boolean, allDone: boolean): string {
  if (isBusy) return "正在创建项目";
  return allDone ? "创建项目并开始分析" : "上传图片并创建项目";
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
          <h2 className="type-section-title mt-2 md:text-[22px]">
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
        "font-mono text-[10px] uppercase tracking-[0.22em] tabular-nums",
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
          className="pointer-events-none absolute inset-x-0 top-0 h-20 bg-gradient-to-b from-black/55 to-transparent"
        />

        {/* uploading overlay */}
        {item.status === "uploading" ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/55 backdrop-blur-sm">
            <Loader2 className="h-5 w-5 animate-spin text-white" />
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] tabular-nums text-white">
              {Math.round(item.progress * 100).toString().padStart(2, "0")}%
            </p>
          </div>
        ) : null}

        {/* error overlay */}
        {item.status === "error" ? (
          <div
            role="alert"
            className="absolute inset-x-0 bottom-0 bg-[var(--danger)]/90 px-3 py-2"
          >
            <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-white/90">
              失败
            </p>
            <p className="mt-1 line-clamp-2 text-[12px] leading-[1.4] text-white">
              {item.error}
            </p>
          </div>
        ) : null}

        {/* top-left N° + main chip */}
        <div className="absolute left-3 top-3 flex items-center gap-2">
          <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-white/85 mix-blend-difference">
            {num}
          </span>
          {isMain ? (
            <span className="rounded-full bg-[var(--accent)] px-2 py-0.5 font-mono text-[9px] uppercase tracking-[0.22em] text-black">
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
      <div className="mt-3 flex min-w-0 items-baseline justify-between gap-3 font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        <span className={cn("truncate", presentation.textClass)} title={item.file.name}>
          {presentation.label}
        </span>
        <span className="shrink-0 tabular-nums">{formatBytes(item.file.size)}</span>
      </div>
      <p
        className="mt-1 truncate text-[12px] text-[var(--fg-1)]"
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
        textClass: "text-[var(--amber-300)]",
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
        borderClass: "border-[var(--danger)]/40",
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
      <div className="flex gap-0.5 rounded-full border border-white/15 bg-black/55 p-0.5 backdrop-blur md:flex-col md:gap-1 md:p-1">
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
    <button
      type="button"
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex h-11 w-11 items-center justify-center rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-30 md:h-8 md:w-8",
        danger
          ? "text-white/85 hover:bg-[var(--danger)]/70 hover:text-white"
          : "text-white/85 hover:bg-white/15 hover:text-white",
      )}
    >
      {children}
    </button>
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
      <span className="flex items-baseline gap-2 font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-2)]">
        <span>{label}</span>
        {chineseLabel && chineseLabel !== label ? (
          <span className="normal-case tracking-normal text-[var(--fg-3)]">
            {chineseLabel}
          </span>
        ) : null}
      </span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-2 h-11 w-full appearance-none border-b border-[var(--border)] bg-transparent bg-[length:14px_14px] bg-[right_4px_center] bg-no-repeat pl-1 pr-6 text-[16px] text-[var(--fg-0)] outline-none transition-colors focus:border-[var(--amber-400)] md:h-10 md:text-[15px]"
        style={{
          backgroundImage:
            "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 14 14' fill='none' stroke='%23999' stroke-width='1.5'%3E%3Cpath d='M3 5l4 4 4-4'/%3E%3C/svg%3E\")",
        }}
      >
        {options.map(([text, optionValue]) => (
          <option key={`${label}-${text}`} value={optionValue} className="bg-[var(--bg-1)]">
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}
