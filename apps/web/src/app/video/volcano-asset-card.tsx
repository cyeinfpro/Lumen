"use client";

/* eslint-disable @next/next/no-img-element */

import { useState } from "react";
import {
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
  Trash2,
  Video,
} from "lucide-react";

import { IconButton } from "@/components/ui/primitives";
import type { VideoAssetOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  volcanoAssetMediaUrl,
  volcanoAssetStatusKind,
} from "./volcano-asset-domain";
import {
  formatTime,
  statusPresentation,
} from "./volcano-asset-manager-helpers";
import type { OperationItem } from "./volcano-asset-manager-types";

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
    <div className="absolute right-1 top-1 z-[var(--z-header)] flex">
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
