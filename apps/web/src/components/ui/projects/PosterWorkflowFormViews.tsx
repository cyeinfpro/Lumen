import type { ReactNode } from "react";
import { ArrowRight, Palette, X } from "lucide-react";
import Image from "next/image";

import { Button } from "@/components/ui/primitives/Button";
import type { PosterStyleItem } from "@/lib/apiClient";
import { cn } from "@/lib/utils";

export function PosterStyleSection({
  style,
  onOpen,
  onClear,
}: {
  style: PosterStyleItem | null;
  onOpen: () => void;
  onClear: () => void;
}) {
  return (
    <>
      <SectionHeader
        eyebrow="N°02 — 风格"
        title="海报风格"
        trailing={
          <Button
            variant="outline"
            size="sm"
            onClick={onOpen}
            leftIcon={<Palette className="h-3.5 w-3.5" />}
          >
            {style ? "更换风格" : "选择风格"}
          </Button>
        }
      />
      <div className="-mt-2">
        {style ? (
          <StyleSummary style={style} onClear={onClear} />
        ) : (
          <button
            type="button"
            onClick={onOpen}
            className="flex min-h-[120px] w-full flex-col items-center justify-center gap-2 rounded-[var(--radius-card)] border border-dashed border-[var(--border-strong)] px-3 text-center transition-colors hover:border-accent-border hover:bg-accent-soft"
          >
            <Palette className="h-5 w-5 text-[var(--fg-2)]" />
            <p className="type-caption text-[var(--fg-1)]">
              点击选择风格
            </p>
            <p className="type-caption text-[var(--fg-3)]">
              没有合适的风格？可去「风格库」创建。
            </p>
          </button>
        )}
      </div>
    </>
  );
}

export function PosterCreateButton({
  pending,
  disabled,
  mobile = false,
  onClick,
}: {
  pending: boolean;
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
        fullWidth={mobile}
        onClick={onClick}
        disabled={disabled}
        loading={pending}
        rightIcon={!mobile && !pending ? <ArrowRight className="h-4 w-4" /> : undefined}
        className={cn(!mobile && "px-6")}
      >
        <span>{pending ? "项目创建中" : "创建海报"}</span>
      </Button>
    </div>
  );
}

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
          <p className="type-caption">{eyebrow}</p>
          <h2 className="type-section-title mt-2 ">{title}</h2>
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

export function StyleSummary({
  style,
  onClear,
}: {
  style: PosterStyleItem;
  onClear: () => void;
}) {
  const coverUrl =
    style.display_url || style.cover_image_url || style.thumb_url || "";
  return (
    <div className="grid min-w-0 grid-cols-[72px_minmax(0,1fr)_auto] gap-3 border-b border-[var(--border)] pb-3 sm:grid-cols-[88px_minmax(0,1fr)_auto]">
      <div className="relative aspect-square overflow-hidden bg-[var(--bg-2)]">
        {coverUrl ? (
          <Image
            src={coverUrl}
            alt={style.title}
            fill
            sizes="88px"
            unoptimized
            className="h-full w-full object-cover"
          />
        ) : null}
      </div>
      <div className="min-w-0">
        <p className="line-clamp-1 type-body-sm font-medium tracking-tight text-[var(--fg-0)]">
          {style.title}
        </p>
        {style.mood ? (
          <p className="mt-1 line-clamp-1 type-caption text-[var(--fg-2)]">
            {style.mood}
          </p>
        ) : null}
        {style.style_tags.length ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {style.style_tags.slice(0, 6).map((tag) => (
              <span
                key={tag}
                className="inline-flex max-w-full items-center rounded-full border border-[var(--border)] px-2 py-0.5 type-caption text-[var(--fg-2)]"
              >
                <span className="truncate">{tag}</span>
              </span>
            ))}
          </div>
        ) : null}
      </div>
      <button
        type="button"
        onClick={onClear}
        aria-label="清除选择"
        className="inline-flex h-11 w-11 cursor-pointer items-center justify-center rounded-full text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] md:h-8 md:w-8"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
