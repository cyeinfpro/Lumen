"use client";

/* eslint-disable @next/next/no-img-element */

import { useState, type DragEvent, type ReactNode } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  ExternalLink,
  FileImage,
  FileVideo,
  Image as ImageIcon,
  Link2,
  Loader2,
  Pencil,
  Play,
  RefreshCw,
  RotateCcw,
  Trash2,
  UploadCloud,
  Video,
  X,
} from "lucide-react";

import {
  Button,
  EmptyState,
  IconButton,
  Input,
  Textarea,
} from "@/components/ui/primitives";
import type {
  VideoAssetCapabilitiesOut,
  VideoAssetOut,
} from "@/lib/types";
import { cn } from "@/lib/utils";

import {
  VOLCANO_ASSET_NAME_MAX_LENGTH,
  volcanoAssetMediaUrl,
  volcanoAssetStatusKind,
  volcanoOperationBlocksMutation,
  volcanoOperationStageMessage,
  volcanoQuotaUsage,
} from "./volcano-asset-domain";
import {
  capabilityCopy,
  formatTime,
  statusPresentation,
  uploadPresentation,
} from "./volcano-asset-manager-helpers";
import {
  uploadCanBeRemoved,
  uploadNameIsEditable,
} from "./volcano-asset-manager-state";
import type {
  GroupFormState,
  OperationItem,
  UploadItem,
} from "./volcano-asset-manager-types";

export function LoadingPanel({
  label,
  compact = false,
}: {
  label: string;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex items-center justify-center gap-2 type-body-sm text-[var(--fg-2)]",
        compact ? "min-h-40" : "min-h-[360px]",
      )}
      aria-busy="true"
    >
      <Loader2 className="h-5 w-5 animate-spin" />
      {label}
    </div>
  );
}

export function CapabilityUnavailable({
  capability,
}: {
  capability: VideoAssetCapabilitiesOut;
}) {
  const copy = capabilityCopy(capability);
  return (
    <EmptyState
      icon={<AlertCircle className="h-5 w-5" />}
      title={copy.title}
      description={
        <span className="space-y-2">
          <span className="block">{copy.description}</span>
          <span className="block text-[var(--fg-0)]">{copy.action}</span>
        </span>
      }
    />
  );
}

export function MetaRow({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="flex min-w-0 items-center justify-between gap-3">
      <dt className="shrink-0 text-[var(--fg-2)]">{label}</dt>
      <dd className="min-w-0 truncate font-mono text-[var(--fg-0)]">
        {value}
      </dd>
    </div>
  );
}

export function InlineMessage({
  tone,
  className,
  children,
}: {
  tone: "error" | "status";
  className?: string;
  children: ReactNode;
}) {
  if (tone === "error") {
    return (
      <div
        role="alert"
        aria-live="assertive"
        className={cn(
          "flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-3 py-2 type-caption text-danger",
          className,
        )}
      >
        <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <span>{children}</span>
      </div>
    );
  }
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        "flex items-start gap-2 rounded-[var(--radius-card)] border border-info-border bg-info-soft px-3 py-2 type-caption text-info",
        className,
      )}
    >
      <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>{children}</span>
    </div>
  );
}

export function ProjectQuotaBadge({
  label,
  used,
  limit,
  loading,
}: {
  label: string;
  used: number | null;
  limit: number;
  loading: boolean;
}) {
  const usage = used == null ? null : volcanoQuotaUsage(used, limit);
  const className = usage?.reached
    ? "border-danger-border bg-danger-soft text-danger"
    : usage && usage.remaining <= 5
      ? "border-warning-border bg-warning-soft text-warning"
      : "border-[var(--border)] bg-[var(--bg-0)]/72 text-[var(--fg-1)]";
  return (
    <span
      role="status"
      aria-label={`${label} ${
        loading || !usage
          ? "读取中"
          : `已用 ${usage.used}，上限 ${usage.limit}，剩余 ${usage.remaining}`
      }`}
      title={
        loading || !usage
          ? `${label}读取中`
          : `${label}剩余 ${usage.remaining}`
      }
      className={cn(
        "inline-flex min-h-8 items-center gap-1.5 rounded-[var(--radius-control)] border px-2.5 type-caption",
        className,
      )}
    >
      <span>{label}</span>
      <span className="font-mono tabular-nums text-[var(--fg-0)]">
        {loading || !usage ? "--" : `${usage.used}/${usage.limit}`}
      </span>
    </span>
  );
}

type OperationActivityPresentation = {
  kind: "pending" | "succeeded" | "paused" | "failed";
  message: string;
  canRetry: boolean;
  retryIsRemote: boolean;
};

function operationActivityPresentation(
  operation: OperationItem,
): OperationActivityPresentation {
  const paused =
    operation.phase === "paused" || operation.phase === "uncertain";
  const canRetry =
    (operation.phase === "failed" && operation.retryable) || paused;
  let kind: OperationActivityPresentation["kind"] = "failed";
  if (operation.phase === "pending") kind = "pending";
  if (operation.phase === "succeeded") kind = "succeeded";
  if (paused) kind = "paused";

  let message = operation.error || "失败";
  if (operation.phase === "pending") {
    message = operation.progressStage
      ? volcanoOperationStageMessage(operation.progressStage)
      : operation.pendingLabel;
  }
  if (operation.phase === "succeeded") message = "已完成";
  return {
    kind,
    message,
    canRetry,
    retryIsRemote: operation.recovery === "retry",
  };
}

function OperationStatusIcon({
  kind,
}: {
  kind: OperationActivityPresentation["kind"];
}) {
  if (kind === "pending") {
    return <Loader2 className="h-4 w-4 animate-spin" />;
  }
  if (kind === "succeeded") {
    return <CheckCircle2 className="h-4 w-4" />;
  }
  if (kind === "paused") {
    return <Clock className="h-4 w-4" />;
  }
  return <AlertCircle className="h-4 w-4" />;
}

function OperationActivityRow({
  operation,
  onRetry,
  onDismiss,
}: {
  operation: OperationItem;
  onRetry: (operationId: string) => void;
  onDismiss: (operationId: string) => void;
}) {
  const presentation = operationActivityPresentation(operation);
  return (
    <div className="flex min-h-11 items-center gap-3 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-1)] px-3 py-2">
      <span
        className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)]",
          presentation.kind === "pending"
            ? "bg-info-soft text-info"
            : presentation.kind === "succeeded"
              ? "bg-success-soft text-success"
              : presentation.kind === "paused"
                ? "bg-warning-soft text-warning"
                : "bg-danger-soft text-danger",
        )}
      >
        <OperationStatusIcon kind={presentation.kind} />
      </span>
      <div className="min-w-0 flex-1">
        <p className="type-body-sm truncate text-[var(--fg-0)]">
          {operation.title}
        </p>
        <p
          className={cn(
            "type-caption truncate",
            operation.phase === "failed"
              ? "text-danger"
              : presentation.kind === "paused"
                ? "text-warning"
                : "text-[var(--fg-2)]",
          )}
        >
          {presentation.message}
        </p>
      </div>
      {presentation.canRetry ? (
        <Button
          variant="outline"
          size="sm"
          leftIcon={
            presentation.retryIsRemote ? (
              <RotateCcw className="h-3.5 w-3.5" />
            ) : (
              <RefreshCw className="h-3.5 w-3.5" />
            )
          }
          onClick={() => onRetry(operation.id)}
        >
          {presentation.retryIsRemote ? "重试" : "检查状态"}
        </Button>
      ) : null}
      {!volcanoOperationBlocksMutation(operation) ? (
        <IconButton
          aria-label={`移除操作记录 ${operation.title}`}
          tooltip="移除记录"
          variant="ghost"
          size="sm"
          onClick={() => onDismiss(operation.id)}
        >
          <X className="h-3.5 w-3.5" />
        </IconButton>
      ) : null}
    </div>
  );
}

export function OperationActivity({
  operations,
  onRetry,
  onDismiss,
}: {
  operations: OperationItem[];
  onRetry: (operationId: string) => void;
  onDismiss: (operationId: string) => void;
}) {
  if (operations.length === 0) return null;
  const visibleOperations = [
    ...operations.filter(volcanoOperationBlocksMutation),
    ...operations.filter(
      (operation) => !volcanoOperationBlocksMutation(operation),
    ),
  ].slice(0, 8);
  return (
    <section
      aria-label="活动操作"
      className="mb-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-0)]/72 p-3"
    >
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="type-card-title">活动操作</p>
          <p className="type-caption mt-0.5 text-[var(--fg-2)]">
            后台执行，不影响继续浏览和管理其他素材
          </p>
        </div>
        <span className="type-caption text-[var(--fg-2)]">
          {operations.filter(volcanoOperationBlocksMutation).length} 个进行中
        </span>
      </div>
      <div className="mt-3 space-y-2" aria-live="polite">
        {visibleOperations.map((operation) => (
          <OperationActivityRow
            key={operation.id}
            operation={operation}
            onRetry={onRetry}
            onDismiss={onDismiss}
          />
        ))}
      </div>
    </section>
  );
}

export function GroupEditor({
  form,
  projectName,
  error,
  onChange,
  onCancel,
  onSave,
}: {
  form: GroupFormState;
  projectName: string;
  error: string | null;
  onChange: (form: GroupFormState) => void;
  onCancel: () => void;
  onSave: () => void;
}) {
  return (
    <div className="mt-3 space-y-3 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] p-3">
      <div>
        <p className="type-card-title">
          {form.mode === "create" ? "新建 AIGC 组" : "编辑素材组"}
        </p>
        <p className="type-caption mt-1 text-[var(--fg-2)]">
          GroupType 固定 AIGC · ProjectName {projectName}
        </p>
      </div>
      <Input
        label="名称"
        value={form.name}
        maxLength={VOLCANO_ASSET_NAME_MAX_LENGTH}
        onChange={(event) =>
          onChange({ ...form, name: event.target.value })
        }
      />
      <Textarea
        label="描述"
        value={form.description}
        maxLength={300}
        rows={3}
        onChange={(event) =>
          onChange({ ...form, description: event.target.value })
        }
      />
      {error ? (
        <div role="alert" aria-live="assertive">
          <InlineMessage tone="error">{error}</InlineMessage>
        </div>
      ) : null}
      <div className="grid grid-cols-2 gap-2">
        <Button variant="ghost" size="sm" onClick={onCancel}>
          取消
        </Button>
        <Button variant="primary" size="sm" onClick={onSave}>
          保存
        </Button>
      </div>
    </div>
  );
}

export function SegmentedFilter({
  value,
  options,
  onChange,
}: {
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
}) {
  return (
    <div
      role="group"
      aria-label="筛选素材类型"
      className="control-shell grid min-h-11 grid-cols-3 p-1 sm:min-h-9"
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            className={cn(
              "min-h-9 min-w-12 rounded-[var(--radius-control)] px-2 type-caption focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]",
              active
                ? "bg-[var(--accent)] text-[var(--accent-on)]"
                : "text-[var(--fg-1)] hover:bg-[var(--bg-2)] hover:text-[var(--fg-0)]",
            )}
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function AssetPagination({
  page,
  totalPages,
  totalCount,
  onPrevious,
  onNext,
}: {
  page: number;
  totalPages: number;
  totalCount: number;
  onPrevious: () => void;
  onNext: () => void;
}) {
  return (
    <nav
      aria-label="火山虚拟素材分页"
      className="mt-4 flex flex-col gap-2 border-t border-[var(--border)] pt-3 min-[420px]:flex-row min-[420px]:items-center min-[420px]:justify-between"
    >
      <p className="type-caption text-[var(--fg-2)]">
        第 {page} / {totalPages} 页 · 共 {totalCount} 个
      </p>
      <div className="grid grid-cols-2 gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={page <= 1}
          onClick={onPrevious}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={page >= totalPages}
          onClick={onNext}
        >
          下一页
        </Button>
      </div>
    </nav>
  );
}

function UploadFileTypeIcon({
  assetType,
}: {
  assetType: UploadItem["assetType"];
}) {
  return assetType === "Image" ? (
    <FileImage className="h-4 w-4" />
  ) : (
    <FileVideo className="h-4 w-4" />
  );
}

function UploadRowMessages({ item }: { item: UploadItem }) {
  const showProgress =
    Boolean(item.progressStage) &&
    (item.phase === "processing" || item.phase === "needs_refresh");
  return (
    <>
      <p className="type-caption mt-1 truncate text-[var(--fg-2)]">
        原文件：{item.fileName}
      </p>
      {showProgress ? (
        <p className="type-caption mt-1 text-[var(--fg-2)]">
          {volcanoOperationStageMessage(item.progressStage || "")}
        </p>
      ) : null}
      {item.error ? (
        <p role="alert" className="type-caption mt-1 break-words text-danger">
          {item.error}
        </p>
      ) : null}
    </>
  );
}

function UploadRetryAction({
  item,
  canRetry,
  onRetry,
}: {
  item: UploadItem;
  canRetry: boolean;
  onRetry: (id: string) => void;
}) {
  if (!canRetry) return null;
  const refresh = item.retryMode === "refresh";
  return (
    <IconButton
      aria-label={`${refresh ? "检查状态" : "重试上传"} ${
        item.name || item.fileName
      }`}
      tooltip={refresh ? "检查状态" : "重试"}
      variant="ghost"
      size="sm"
      onClick={() => onRetry(item.id)}
    >
      {refresh ? (
        <RefreshCw className="h-3.5 w-3.5" />
      ) : (
        <RotateCcw className="h-3.5 w-3.5" />
      )}
    </IconButton>
  );
}

function UploadRow({
  item,
  operationBlocked,
  onRename,
  onRemove,
  onRetry,
}: {
  item: UploadItem;
  operationBlocked: boolean;
  onRename: (id: string, name: string) => void;
  onRemove: (id: string) => void;
  onRetry: (id: string) => void;
}) {
  const presentation = uploadPresentation(item.phase);
  const busy = [
    "uploading",
    "optimizing",
    "waiting_quota",
    "processing",
  ].includes(item.phase);
  const nameEditable = uploadNameIsEditable(item) && !operationBlocked;
  const canRetry =
    item.phase === "needs_refresh" ||
    (item.phase === "failed" && item.retryMode !== "none");
  const removeDisabled = !uploadCanBeRemoved(item) || operationBlocked;
  return (
    <div className="grid min-h-11 gap-2 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] px-3 py-2 sm:grid-cols-[auto_minmax(0,1fr)_auto_auto] sm:items-center">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-[var(--bg-2)] text-[var(--fg-1)]">
        <UploadFileTypeIcon assetType={item.assetType} />
      </span>
      <div className="min-w-0 flex-1">
        <label className="type-caption text-[var(--fg-2)]">
          素材名称
          <input
            type="text"
            value={item.name}
            maxLength={VOLCANO_ASSET_NAME_MAX_LENGTH}
            disabled={!nameEditable}
            aria-label={`素材名称 ${item.fileName}`}
            aria-invalid={!item.name.trim() || undefined}
            className="mt-1 h-9 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 type-body-sm text-[var(--fg-0)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)] disabled:opacity-75 max-sm:min-h-11"
            onChange={(event) => onRename(item.id, event.target.value)}
          />
        </label>
        <UploadRowMessages item={item} />
      </div>
      <span
        className={cn(
          "inline-flex shrink-0 items-center gap-1 rounded-[var(--radius-control)] border px-2 py-1 type-caption",
          presentation.className,
        )}
      >
        {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
        {presentation.label}
      </span>
      <UploadRetryAction
        item={item}
        canRetry={canRetry}
        onRetry={onRetry}
      />
      <IconButton
        aria-label={`从上传列表移除 ${item.name || item.fileName}`}
        tooltip={
          removeDisabled ? "等待后台结果后可移除" : "移除列表，不删除云端"
        }
        variant="ghost"
        size="sm"
        disabled={removeDisabled}
        onClick={() => onRemove(item.id)}
      >
        <X className="h-3.5 w-3.5" />
      </IconButton>
    </div>
  );
}

export function UploadArea({
  inputId,
  dragActive,
  uploads,
  blockedUploadIds,
  disabledReason,
  pendingAssetCreates,
  onDragActive,
  onFiles,
  onRename,
  onRemove,
  onRetry,
}: {
  inputId: string;
  dragActive: boolean;
  uploads: UploadItem[];
  blockedUploadIds: ReadonlySet<string>;
  disabledReason: string | null;
  pendingAssetCreates: number;
  onDragActive: (active: boolean) => void;
  onFiles: (files: File[]) => void;
  onRename: (id: string, name: string) => void;
  onRemove: (id: string) => void;
  onRetry: (id: string) => void;
}) {
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    onDragActive(false);
    if (disabledReason) return;
    onFiles(Array.from(event.dataTransfer.files));
  };
  return (
    <section aria-label="上传火山虚拟素材">
      <div
        className={cn(
          "relative flex min-h-20 items-center rounded-[var(--radius-card)] border border-dashed px-4 py-3 transition-colors",
          "focus-within:border-[var(--accent)] focus-within:ring-2 focus-within:ring-[var(--accent)]/35",
          dragActive
            ? "border-accent-border bg-accent-soft"
            : disabledReason
              ? "border-[var(--border)] bg-[var(--bg-0)]/45 opacity-75"
              : "border-[var(--border-strong)] bg-[var(--bg-0)]/70 hover:bg-[var(--bg-2)]",
        )}
        aria-disabled={disabledReason ? "true" : undefined}
        onDragEnter={(event) => {
          event.preventDefault();
          if (disabledReason) return;
          onDragActive(true);
        }}
        onDragOver={(event) => {
          event.preventDefault();
          if (disabledReason) return;
          onDragActive(true);
        }}
        onDragLeave={(event) => {
          if (event.currentTarget.contains(event.relatedTarget as Node)) return;
          onDragActive(false);
        }}
        onDrop={onDrop}
      >
        <input
          id={inputId}
          type="file"
          multiple
          accept=".png,.jpg,.jpeg,.webp,.mp4,.mov,image/png,image/jpeg,image/webp,video/mp4,video/quicktime"
          aria-label="选择火山虚拟素材文件"
          disabled={Boolean(disabledReason)}
          className="absolute inset-0 z-10 cursor-pointer opacity-0 focus-visible:outline-none disabled:cursor-not-allowed"
          onChange={(event) => {
            onFiles(Array.from(event.target.files ?? []));
            event.target.value = "";
          }}
        />
        <div className="pointer-events-none flex w-full min-w-0 items-center gap-3">
          <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-[var(--radius-control)] bg-accent-soft text-accent">
            <UploadCloud className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <p className="type-body-sm text-[var(--fg-0)]">
              {disabledReason || "上传素材"}
            </p>
            <p className="type-caption mt-0.5 text-[var(--fg-2)]">
              图片 ≤ 50 MiB · 视频 ≤ 64 MiB
            </p>
          </div>
          {pendingAssetCreates > 0 ? (
            <span className="ml-auto shrink-0 rounded-[var(--radius-control)] border border-warning-border bg-warning-soft px-2 py-1 type-caption text-warning">
              队列 {pendingAssetCreates}
            </span>
          ) : null}
        </div>
      </div>

      {uploads.length > 0 ? (
        <div className="mt-3 space-y-2" aria-live="polite">
          {uploads.map((item) => (
            <UploadRow
              key={item.id}
              item={item}
              operationBlocked={blockedUploadIds.has(item.id)}
              onRename={onRename}
              onRemove={onRemove}
              onRetry={onRetry}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}

type AssetCardProps = {
  asset: VideoAssetOut;
  selected: boolean;
  existing: boolean;
  pendingOperation?: OperationItem;
  atLimit: boolean;
  onToggle: () => void;
  onRename: () => void;
  onDelete: () => void;
};

function AssetMedia({ asset }: { asset: VideoAssetOut }) {
  const mediaUrl = volcanoAssetMediaUrl(asset);
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const failed = Boolean(mediaUrl && failedUrl === mediaUrl);
  if (!mediaUrl || failed) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 text-[var(--fg-2)]">
        {asset.asset_type === "Image" ? (
          <ImageIcon className="h-8 w-8" />
        ) : (
          <Video className="h-8 w-8" />
        )}
        <span className="type-caption">
          {failed ? "预览加载失败" : "暂无预览"}
        </span>
      </div>
    );
  }
  if (asset.asset_type === "Image") {
    return (
      <img
        src={mediaUrl}
        alt={`${asset.name || "虚拟素材"}预览`}
        className="h-full w-full object-cover"
        loading="lazy"
        onError={() => setFailedUrl(mediaUrl)}
      />
    );
  }
  return (
    <div className="relative h-full w-full">
      <video
        src={mediaUrl}
        aria-label={`${asset.name || "虚拟素材"}视频预览`}
        className="h-full w-full object-cover"
        muted
        playsInline
        preload="metadata"
        onError={() => setFailedUrl(mediaUrl)}
      />
      <span className="absolute bottom-2 right-2 inline-flex h-8 w-8 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--bg-0)]/82 text-[var(--fg-0)] shadow-[var(--shadow-1)] backdrop-blur-sm">
        <Play className="h-3.5 w-3.5 fill-current" />
      </span>
    </div>
  );
}

function AssetCardBadges({
  asset,
  selected,
  existing,
  pendingOperation,
}: Pick<
  AssetCardProps,
  "asset" | "selected" | "existing" | "pendingOperation"
>) {
  const status = statusPresentation(asset.status);
  const stateBadge = selected ? (
    <span className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-accent-border bg-[var(--accent)] px-2 py-1 type-caption text-[var(--accent-on)]">
      <CheckCircle2 className="h-3 w-3" />
      已选
    </span>
  ) : existing ? (
    <span className="rounded-[var(--radius-control)] border border-info-border bg-info-soft px-2 py-1 type-caption text-info">
      草稿已用
    </span>
  ) : pendingOperation ? (
    <span className="inline-flex items-center gap-1 rounded-[var(--radius-control)] border border-warning-border bg-warning-soft px-2 py-1 type-caption text-warning">
      <Loader2 className="h-3 w-3 animate-spin" />
      处理中
    </span>
  ) : null;
  return (
    <>
      {stateBadge ? (
        <div className="absolute left-2 top-2">{stateBadge}</div>
      ) : null}
      <span
        className={cn(
          "absolute bottom-2 left-2 inline-flex rounded-[var(--radius-control)] border px-2 py-1 type-caption backdrop-blur-sm",
          status.className,
        )}
      >
        {status.label}
      </span>
    </>
  );
}

function AssetCardActions({
  asset,
  disabled,
  onRename,
  onDelete,
}: Pick<AssetCardProps, "asset" | "onRename" | "onDelete"> & {
  disabled: boolean;
}) {
  return (
    <div className="absolute right-1 top-1 z-20 flex">
      <IconButton
        aria-label={`重命名云端素材 ${asset.name || "未命名素材"}`}
        tooltip="重命名"
        variant="secondary"
        size="sm"
        disabled={disabled}
        onClick={onRename}
      >
        <Pencil className="h-3.5 w-3.5" />
      </IconButton>
      <IconButton
        aria-label={`删除云端素材 ${asset.name || "未命名素材"}`}
        tooltip="删除云端素材"
        variant="secondary"
        size="sm"
        disabled={disabled}
        onClick={onDelete}
      >
        <Trash2 className="h-3.5 w-3.5" />
      </IconButton>
    </div>
  );
}

function AssetKindLabel({ asset }: { asset: VideoAssetOut }) {
  return (
    <span className="inline-flex items-center gap-1">
      {asset.asset_type === "Image" ? (
        <FileImage className="h-3.5 w-3.5" />
      ) : (
        <FileVideo className="h-3.5 w-3.5" />
      )}
      {asset.asset_type === "Image" ? "图片" : "视频"}
    </span>
  );
}

function AssetCardDetails({
  asset,
  selected,
  existing,
  pendingOperation,
  atLimit,
}: Pick<
  AssetCardProps,
  "asset" | "selected" | "existing" | "pendingOperation" | "atLimit"
>) {
  const showLimit =
    !selected &&
    atLimit &&
    volcanoAssetStatusKind(asset.status) === "active" &&
    !existing;
  return (
    <>
      <p className="type-body-sm truncate text-[var(--fg-0)]">
        {asset.name || "未命名素材"}
      </p>
      {pendingOperation ? (
        <p className="type-caption mt-1 truncate text-warning">
          {pendingOperation.pendingLabel}
        </p>
      ) : null}
      <div className="mt-1 flex items-center justify-between gap-2 type-caption text-[var(--fg-2)]">
        <AssetKindLabel asset={asset} />
        <span className="inline-flex min-w-0 items-center gap-1 truncate">
          <Clock className="h-3.5 w-3.5 shrink-0" />
          {formatTime(asset.update_time || asset.create_time)}
        </span>
      </div>
      {showLimit ? (
        <p className="type-caption mt-1 text-warning">本类型已达选择上限</p>
      ) : null}
    </>
  );
}

function AssetCardLink({ asset }: { asset: VideoAssetOut }) {
  const mediaUrl = volcanoAssetMediaUrl(asset);
  return (
    <div className="border-t border-[var(--border-subtle)] px-3 py-2">
      {mediaUrl ? (
        <a
          href={mediaUrl}
          target="_blank"
          rel="noopener noreferrer"
          title={mediaUrl}
          className="inline-flex max-w-full items-center gap-1.5 type-caption text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
        >
          <Link2 className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">
            {asset.url ? "火山素材链接" : "安全预览链接"}
          </span>
          <ExternalLink className="h-3 w-3 shrink-0" />
        </a>
      ) : (
        <span className="inline-flex items-center gap-1.5 type-caption text-[var(--fg-2)]">
          <Link2 className="h-3.5 w-3.5" />
          暂无素材链接
        </span>
      )}
    </div>
  );
}

function assetCanToggle({
  asset,
  selected,
  existing,
  pendingOperation,
  atLimit,
}: Pick<
  AssetCardProps,
  "asset" | "selected" | "existing" | "pendingOperation" | "atLimit"
>): boolean {
  if (pendingOperation) return false;
  if (selected) return true;
  return (
    volcanoAssetStatusKind(asset.status) === "active" &&
    !existing &&
    !atLimit
  );
}

export function AssetCard(props: AssetCardProps) {
  const {
    asset,
    selected,
    existing,
    pendingOperation,
    atLimit,
    onToggle,
    onRename,
    onDelete,
  } = props;
  const canToggle = assetCanToggle(props);
  const name = asset.name || "未命名素材";
  return (
    <article
      className={cn(
        "relative overflow-hidden rounded-[var(--radius-card)] border bg-[var(--bg-0)] shadow-[var(--shadow-1)] transition-colors",
        selected
          ? "border-accent-border bg-accent-soft ring-2 ring-[var(--accent)]/25"
          : "border-[var(--border)] hover:border-[var(--border-strong)]",
      )}
    >
      <button
        type="button"
        aria-label={selected ? `取消选择 ${name}` : `选择 ${name}`}
        aria-pressed={selected}
        aria-disabled={!canToggle}
        disabled={!canToggle}
        className="block w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)] disabled:cursor-not-allowed"
        onClick={onToggle}
      >
        <div className="relative aspect-[4/3] overflow-hidden bg-[var(--bg-2)]">
          <AssetMedia asset={asset} />
          <AssetCardBadges
            asset={asset}
            selected={selected}
            existing={existing}
            pendingOperation={pendingOperation}
          />
        </div>
        <div className="min-h-20 px-3 py-2">
          <AssetCardDetails
            asset={asset}
            selected={selected}
            existing={existing}
            pendingOperation={pendingOperation}
            atLimit={atLimit}
          />
        </div>
      </button>
      <AssetCardActions
        asset={asset}
        disabled={Boolean(pendingOperation)}
        onRename={onRename}
        onDelete={onDelete}
      />
      <AssetCardLink asset={asset} />
    </article>
  );
}
