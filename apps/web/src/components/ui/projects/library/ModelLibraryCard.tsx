"use client";

import {
  Bookmark,
  Check,
  CheckSquare,
  Sparkles,
  Square,
  Trash2,
} from "lucide-react";
import Image from "next/image";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { toast } from "@/components/ui/primitives/Toast";
import {
  MODEL_LIBRARY_APPEARANCE_LABEL,
  type ApparelModelLibraryItem,
  type ApparelModelLibrarySaveJobItemIn,
  type ModelLibraryAppearance,
} from "@/lib/apiClient";
import {
  useAutoTagApparelModelLibraryItemMutation,
  useSaveApparelModelLibraryJobItemMutation,
} from "@/lib/queries";
import { cn } from "@/lib/utils";

import { AGE_LABEL, SOURCE_LABEL_SHORT } from "./modelLibraryBrowserOptions";

interface LoserItemIdentity {
  isLoser: boolean;
  workflowRunId: string;
  imageId: string;
}

function loserItemIdentity(
  item: ApparelModelLibraryItem,
  loserItem: ApparelModelLibraryItem | undefined,
): LoserItemIdentity {
  if (!loserItem) {
    return { isLoser: false, workflowRunId: "", imageId: "" };
  }
  const parts = item.id.split(":");
  if (parts.length < 3 || parts[0] !== "loser") {
    return { isLoser: true, workflowRunId: "", imageId: "" };
  }
  return {
    isLoser: true,
    workflowRunId: parts[1],
    imageId: parts.slice(2).join(":"),
  };
}

function modelLibraryItemIsFree(
  item: ApparelModelLibraryItem,
  isLoser: boolean,
): boolean {
  return (
    isLoser ||
    item.billing_free === true ||
    item.billing_label === "free" ||
    item.is_dual_race_bonus === true
  );
}

function modelLibraryAppearanceLabel(
  item: ApparelModelLibraryItem,
): string | null {
  if (
    !item.appearance_direction ||
    !(item.appearance_direction in MODEL_LIBRARY_APPEARANCE_LABEL)
  ) {
    return null;
  }
  return MODEL_LIBRARY_APPEARANCE_LABEL[
    item.appearance_direction as Exclude<ModelLibraryAppearance, "all">
  ];
}

function useDeleteConfirmation(onDelete: () => void) {
  const [confirming, setConfirming] = useState(false);
  const timerRef = useRef<number | null>(null);
  const clearTimer = () => {
    if (timerRef.current === null) return;
    window.clearTimeout(timerRef.current);
    timerRef.current = null;
  };
  useEffect(() => clearTimer, []);
  const requestDelete = () => {
    clearTimer();
    if (confirming) {
      onDelete();
      setConfirming(false);
      return;
    }
    setConfirming(true);
    timerRef.current = window.setTimeout(() => {
      setConfirming(false);
      timerRef.current = null;
    }, 3000);
  };
  return { confirming, requestDelete };
}

export function ModelLibraryCard({
  item,
  order,
  highlighted,
  selected,
  deleting,
  onOpenLightbox,
  onDelete,
  onToggleSelected,
  onSaveLoser,
  onSelect,
  selectLabel,
}: {
  item: ApparelModelLibraryItem;
  order: number;
  highlighted: boolean;
  selected: boolean;
  deleting: boolean;
  onOpenLightbox: () => void;
  onDelete: () => void;
  onToggleSelected?: () => void;
  onSaveLoser?: ApparelModelLibraryItem;
  onSelect?: (item: ApparelModelLibraryItem) => void;
  selectLabel?: string;
}) {
  const isPreset = item.source === "preset";
  const loserIdentity = loserItemIdentity(item, onSaveLoser);
  const isFree = modelLibraryItemIsFree(item, loserIdentity.isLoser);
  const { confirming: confirmingDelete, requestDelete } =
    useDeleteConfirmation(onDelete);
  const autoTag = useAutoTagApparelModelLibraryItemMutation(item.id, {
    onSuccess: (data) =>
      toast.success("已识别气质方向", {
        description:
          data.style_tags.length > 0
            ? data.style_tags.join("、")
            : "未识别到明显气质方向",
      }),
    onError: (err) =>
      toast.error("识别失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      }),
  });
  const saveLoser = useSaveApparelModelLibraryJobItemMutation(
    loserIdentity.workflowRunId,
    loserIdentity.imageId,
    {
      onSuccess: () => toast.success("已收藏入库"),
      onError: (err) =>
        toast.error("入库失败", {
          description: err instanceof Error ? err.message : "请稍后重试",
        }),
    },
  );

  const appearanceLabel = modelLibraryAppearanceLabel(item);
  const saveLoserItem = () => {
    if (!loserIdentity.workflowRunId || !loserIdentity.imageId) return;
    const body: ApparelModelLibrarySaveJobItemIn = {
      title: item.title,
      age_segment: item.age_segment,
      gender: item.gender === "male" ? "male" : "female",
      appearance_direction: item.appearance_direction,
      style_tags: item.style_tags,
      auto_tag: true,
    };
    saveLoser.mutate(body);
  };

  return (
    <article
      className="group relative"
      style={{ contentVisibility: "auto", containIntrinsicSize: "1px 360px" }}
    >
      <ModelLibrarySelectionControl
        selected={selected}
        onToggleSelected={onToggleSelected}
      />
      <ModelLibraryCardThumbnail
        appearanceLabel={appearanceLabel}
        highlighted={highlighted}
        isFree={isFree}
        item={item}
        order={order}
        selectionVisible={Boolean(onToggleSelected)}
        onOpenLightbox={onOpenLightbox}
      />
      <ModelLibraryCardMetadata item={item}>
        <ModelLibraryCardActions
          autoTagPending={autoTag.isPending}
          confirmingDelete={confirmingDelete}
          deleting={deleting}
          isLoser={loserIdentity.isLoser}
          isPreset={isPreset}
          item={item}
          saveLoserPending={saveLoser.isPending}
          selectLabel={selectLabel}
          onAutoTag={() => autoTag.mutate()}
          onDelete={requestDelete}
          onSaveLoser={saveLoserItem}
          onSelect={onSelect}
        />
      </ModelLibraryCardMetadata>
    </article>
  );
}

function ModelLibrarySelectionControl({
  selected,
  onToggleSelected,
}: {
  selected: boolean;
  onToggleSelected?: () => void;
}) {
  if (!onToggleSelected) return null;
  return (
    /* @ui-governance-allow media: selection control overlays the model thumbnail. */
    <button
      type="button"
      onClick={onToggleSelected}
      aria-label={selected ? "取消选择" : "选择模特"}
      className={cn(
        // @ui-governance-allow media
        "absolute left-2 top-2 z-10 inline-flex h-11 w-11 items-center justify-center rounded-full border backdrop-blur transition-colors md:h-8 md:w-8",
        selected
          ? "border-[var(--border-amber)] bg-[var(--accent)] text-[var(--accent-on)]"
          : "border-white/40 bg-black/35 text-white hover:bg-black/55",
      )}
    >
      {selected ? (
        <CheckSquare className="h-4 w-4" />
      ) : (
        <Square className="h-4 w-4" />
      )}
    </button>
  );
}

function ModelLibraryCardThumbnail({
  appearanceLabel,
  highlighted,
  isFree,
  item,
  order,
  selectionVisible,
  onOpenLightbox,
}: {
  appearanceLabel: string | null;
  highlighted: boolean;
  isFree: boolean;
  item: ApparelModelLibraryItem;
  order: number;
  selectionVisible: boolean;
  onOpenLightbox: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpenLightbox}
      aria-label={`查看 ${item.title} 大图`}
      className={cn(
        "relative block aspect-[3/4] w-full cursor-zoom-in overflow-hidden rounded-[var(--radius-card)] bg-[var(--bg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--amber-400)]/60",
        highlighted
          ? "outline outline-2 outline-offset-2 outline-[var(--amber-400)]"
          : "",
      )}
    >
      <Image
        src={item.thumb_url || item.image_url}
        alt={item.title}
        fill
        unoptimized
        sizes="(max-width: 640px) 50vw, (max-width: 1024px) 33vw, 240px"
        className="object-cover transition-transform duration-[var(--dur-slow)] ease-[var(--ease-develop)] group-hover:scale-[1.02]"
      />
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-gradient-to-t from-black/60 via-transparent to-transparent opacity-0 transition-opacity duration-[var(--dur-base)] group-hover:opacity-100"
      />
      <span
        className={cn(
          "absolute top-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/85 mix-blend-difference",
          selectionVisible ? "left-11" : "left-2",
        )}
      >
        N°{String(order + 1).padStart(2, "0")}
      </span>
      <span
        className={cn(
          "absolute right-2 top-2 inline-flex items-center font-mono text-[10px]",
          isFree
            ? "rounded-full border border-white/20 bg-black/60 px-2 py-0.5 tracking-[0.14em] text-white backdrop-blur"
            : "uppercase tracking-[0.18em] text-white/85 mix-blend-difference",
        )}
      >
        {isFree ? "free" : SOURCE_LABEL_SHORT[item.source]}
      </span>
      {appearanceLabel ? (
        <span className="absolute bottom-2 right-2 font-mono text-[10px] uppercase tracking-[0.18em] text-white/85 mix-blend-difference">
          {appearanceLabel}
        </span>
      ) : null}
    </button>
  );
}

function ModelLibraryCardMetadata({
  children,
  item,
}: {
  children: ReactNode;
  item: ApparelModelLibraryItem;
}) {
  return (
    <div className="mt-2 grid min-w-0 gap-0.5">
      <p className="line-clamp-1 min-w-0 break-words text-[13px] font-medium leading-[1.3] text-[var(--fg-0)] transition-colors duration-[var(--dur-base)] group-hover:text-[var(--amber-300)]">
        {item.title}
      </p>
      <p className="min-w-0 truncate font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-2)] min-[390px]:tracking-[0.18em]">
        <span>{AGE_LABEL[item.age_segment]}</span>
        {item.gender ? (
          <>
            <span aria-hidden className="mx-1.5 text-[var(--fg-3)]">
              ·
            </span>
            <span>{item.gender === "male" ? "男" : "女"}</span>
          </>
        ) : null}
      </p>
      {item.style_tags.length > 0 ? (
        <p className="line-clamp-1 min-w-0 break-words font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--fg-2)] min-[390px]:tracking-[0.16em]">
          {item.style_tags.slice(0, 3).join(" · ")}
        </p>
      ) : (
        <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-[var(--fg-3)] min-[390px]:tracking-[0.16em]">
          未标记
        </p>
      )}
      {children}
    </div>
  );
}

function ModelLibraryCardActions({
  autoTagPending,
  confirmingDelete,
  deleting,
  isLoser,
  isPreset,
  item,
  onAutoTag,
  onDelete,
  onSaveLoser,
  onSelect,
  saveLoserPending,
  selectLabel,
}: {
  autoTagPending: boolean;
  confirmingDelete: boolean;
  deleting: boolean;
  isLoser: boolean;
  isPreset: boolean;
  item: ApparelModelLibraryItem;
  onAutoTag: () => void;
  onDelete: () => void;
  onSaveLoser: () => void;
  onSelect?: (item: ApparelModelLibraryItem) => void;
  saveLoserPending: boolean;
  selectLabel?: string;
}) {
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
      {onSelect ? (
        <Button
          size="sm"
          variant="primary"
          onClick={() => onSelect(item)}
          leftIcon={<Check className="h-3 w-3" />}
        >
          {selectLabel ?? "设为当前模特"}
        </Button>
      ) : null}
      {isLoser ? (
        <Button
          size="sm"
          variant="primary"
          loading={saveLoserPending}
          onClick={onSaveLoser}
          leftIcon={<Bookmark className="h-3 w-3" />}
        >
          收藏入库
        </Button>
      ) : (
        <ModelLibraryItemActions
          autoTagPending={autoTagPending}
          confirmingDelete={confirmingDelete}
          deleting={deleting}
          isPreset={isPreset}
          onAutoTag={onAutoTag}
          onDelete={onDelete}
        />
      )}
    </div>
  );
}

function ModelLibraryItemActions({
  autoTagPending,
  confirmingDelete,
  deleting,
  isPreset,
  onAutoTag,
  onDelete,
}: {
  autoTagPending: boolean;
  confirmingDelete: boolean;
  deleting: boolean;
  isPreset: boolean;
  onAutoTag: () => void;
  onDelete: () => void;
}) {
  const deleteLabel = confirmingDelete
    ? "再次点击确认删除"
    : isPreset
      ? "隐藏预设"
      : "删除条目";
  return (
    <>
      <button
        type="button"
        onClick={onAutoTag}
        disabled={autoTagPending}
        title="重新识别气质方向"
        aria-label="重新识别气质方向"
        className="inline-flex min-h-11 cursor-pointer items-center gap-1 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)] transition-colors hover:text-[var(--amber-300)] disabled:cursor-not-allowed disabled:opacity-50 md:h-7 md:min-h-0"
      >
        {autoTagPending ? (
          <Spinner size={12} />
        ) : (
          <Sparkles className="h-3 w-3" />
        )}
        识别
      </button>
      <button
        type="button"
        onClick={onDelete}
        disabled={deleting}
        title={deleteLabel}
        aria-label={deleteLabel}
        className={cn(
          "inline-flex min-h-11 cursor-pointer items-center gap-1 font-mono text-[10px] uppercase tracking-[0.16em] transition-colors disabled:cursor-not-allowed disabled:opacity-50 md:h-7 md:min-h-0",
          confirmingDelete
            ? "text-[var(--danger)]"
            : "text-[var(--fg-2)] hover:text-[var(--danger)]",
        )}
      >
        {deleting ? <Spinner size={12} /> : <Trash2 className="h-3 w-3" />}
        {confirmingDelete ? "确认" : isPreset ? "隐藏" : "删除"}
      </button>
    </>
  );
}
