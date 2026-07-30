import type { ReactNode } from "react";
import { ArrowRight, Loader2, Palette, X } from "lucide-react";
import Image from "next/image";

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
          <button
            type="button"
            onClick={onOpen}
            className="inline-flex min-h-11 items-center gap-1.5 border border-[var(--border)] px-3 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--fg-1)] transition-colors hover:border-[var(--border-amber)] hover:text-[var(--amber-300)] md:min-h-9"
          >
            <Palette className="h-3.5 w-3.5" />
            {style ? "更换风格" : "从风格库选择"}
          </button>
        }
      />
      <div className="-mt-2">
        {style ? (
          <StyleSummary style={style} onClear={onClear} />
        ) : (
          <button
            type="button"
            onClick={onOpen}
            className="flex w-full min-h-[120px] flex-col items-center justify-center gap-2 border border-dashed border-[var(--border-strong)] px-3 text-center transition-colors hover:border-[var(--border-amber)] hover:bg-[var(--accent-soft)]"
          >
            <Palette className="h-5 w-5 text-[var(--fg-2)]" />
            <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-[var(--fg-1)]">
              点击选择风格
            </p>
            <p className="text-[12px] text-[var(--fg-3)]">
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
        {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
        <span>{pending ? "创建中" : "创建海报项目"}</span>
        {!mobile && !pending ? (
          <ArrowRight className="h-4 w-4 -translate-x-1 opacity-0 transition-all duration-[var(--dur-base)] group-enabled:group-hover:translate-x-0 group-enabled:group-hover:opacity-100" />
        ) : null}
      </button>
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
          <p className="type-page-kicker">{eyebrow}</p>
          <h2 className="type-section-title mt-2 md:text-[22px]">{title}</h2>
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
        <p className="line-clamp-1 text-[14px] font-medium tracking-tight text-[var(--fg-0)]">
          {style.title}
        </p>
        {style.mood ? (
          <p className="mt-1 line-clamp-1 text-[12px] text-[var(--fg-2)]">
            {style.mood}
          </p>
        ) : null}
        {style.style_tags.length ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {style.style_tags.slice(0, 6).map((tag) => (
              <span
                key={tag}
                className="inline-flex max-w-full items-center rounded-full border border-[var(--border)] px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.16em] text-[var(--fg-2)]"
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
