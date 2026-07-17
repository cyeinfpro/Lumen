"use client";

import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown, Copy } from "lucide-react";
import { useMemo, useState } from "react";
import { copyTextToClipboard } from "@/lib/clipboard";
import { DURATION, EASE } from "@/lib/motion";
import { cn } from "@/lib/utils";
import type { LightboxItem } from "./types";
import {
  buildLightboxMetadataSections,
  getLightboxRevisedPrompt,
  type LightboxMetadataRow,
} from "./utils";

type LightboxDetailsTone = "surface" | "media";

export interface LightboxDetailsContentProps {
  item: LightboxItem;
  tone?: LightboxDetailsTone;
  className?: string;
  onCopyPrompt?: () => void;
}

const TONE = {
  surface: {
    card: "border-[var(--border-subtle)] bg-[var(--bg-2)]/55",
    nested: "border-[var(--border-subtle)] bg-[var(--bg-2)]/45",
    heading: "text-[var(--fg-2)]",
    text: "text-[var(--fg-0)]",
    muted: "text-[var(--fg-1)]",
    button:
      "text-[var(--fg-1)] hover:bg-[var(--bg-3)] hover:text-[var(--fg-0)] active:text-[var(--amber-400)] focus-visible:ring-[var(--amber-400)]/60",
    divider: "border-[var(--border-subtle)]",
    badge:
      "border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/12 text-[var(--fg-0)]",
  },
  media: {
    card: "border-white/10 bg-white/[0.04]",
    nested: "border-white/10 bg-white/[0.04]",
    heading: "text-white/45",
    text: "text-white/84",
    muted: "text-white/72",
    button:
      "border border-white/10 bg-white/5 text-white/72 hover:border-white/25 hover:bg-white/10 hover:text-white focus-visible:ring-[var(--color-lumen-amber)]/70",
    divider: "border-white/10",
    badge:
      "border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/16 text-[var(--amber-100)]",
  },
} satisfies Record<LightboxDetailsTone, Record<string, string>>;

export function LightboxDetailsContent({
  item,
  tone = "surface",
  className,
  onCopyPrompt,
}: LightboxDetailsContentProps) {
  const style = TONE[tone];
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [revisedOpenItemId, setRevisedOpenItemId] = useState<string | null>(null);
  const promptCopied = copiedKey === `${item.id}:prompt`;
  const revisedCopied = copiedKey === `${item.id}:revised`;
  const revisedOpen = revisedOpenItemId === item.id;
  const metadataSections = useMemo(
    () => buildLightboxMetadataSections(item),
    [item],
  );
  const revisedPrompt = useMemo(() => getLightboxRevisedPrompt(item), [item]);

  const clearCopiedSoon = () => {
    window.setTimeout(() => setCopiedKey(null), 1400);
  };

  const handleCopyPrompt = () => {
    if (!item.prompt) return;
    if (onCopyPrompt) {
      onCopyPrompt();
    } else {
      void copyTextToClipboard(item.prompt);
    }
    setCopiedKey(`${item.id}:prompt`);
    clearCopiedSoon();
  };

  const handleCopyRevisedPrompt = () => {
    if (!revisedPrompt) return;
    void copyTextToClipboard(revisedPrompt);
    setCopiedKey(`${item.id}:revised`);
    clearCopiedSoon();
  };

  return (
    <div className={cn("space-y-3.5", className)}>
      {(item.prompt || revisedPrompt) && (
        <section className={cn("rounded-[var(--radius-card)] border p-3", style.card)}>
          {item.prompt && (
            <>
              <div className="mb-1 flex items-center justify-between gap-2">
                <h3
                  className={cn(
                    "font-mono text-[11px] uppercase tracking-wide",
                    style.heading,
                  )}
                >
                  prompt
                </h3>
                <button
                  type="button"
                  onClick={handleCopyPrompt}
                  className={cn(
                    "inline-flex min-h-11 items-center gap-1.5 rounded-full px-2.5 text-[11px] transition-colors",
                    style.button,
                  )}
                  aria-live="polite"
                >
                  {promptCopied ? (
                    <Check className="h-3.5 w-3.5" aria-hidden />
                  ) : (
                    <Copy className="h-3.5 w-3.5" aria-hidden />
                  )}
                  {promptCopied ? "已复制" : "复制"}
                </button>
              </div>
              <p
                className={cn(
                  "whitespace-pre-wrap break-words text-sm leading-relaxed",
                  tone === "surface" && "text-[15px]",
                  style.text,
                )}
              >
                {item.prompt}
              </p>
            </>
          )}

          {revisedPrompt && (
            <div
              className={cn(
                "mt-3 border-t pt-2.5",
                style.divider,
                !item.prompt && "mt-0 border-t-0 pt-0",
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <button
                  type="button"
                  onClick={() =>
                    setRevisedOpenItemId((value) =>
                      value === item.id ? null : item.id,
                    )
                  }
                  aria-expanded={revisedOpen}
                  className={cn(
                    "inline-flex min-h-11 min-w-0 flex-1 items-center gap-2 rounded-[var(--radius-control)] px-2 text-left text-[12px] font-medium transition-colors",
                    style.button,
                  )}
                >
                  <ChevronDown
                    className={cn(
                      "h-3.5 w-3.5 shrink-0 transition-transform",
                      revisedOpen && "rotate-180",
                    )}
                    aria-hidden
                  />
                  <span className="truncate">模型改写后的提示词</span>
                </button>
                <button
                  type="button"
                  onClick={handleCopyRevisedPrompt}
                  className={cn(
                    "inline-flex min-h-11 shrink-0 items-center gap-1.5 rounded-full px-2.5 text-[11px] transition-colors",
                    style.button,
                  )}
                >
                  {revisedCopied ? (
                    <Check className="h-3.5 w-3.5" aria-hidden />
                  ) : (
                    <Copy className="h-3.5 w-3.5" aria-hidden />
                  )}
                  {revisedCopied ? "已复制" : "复制"}
                </button>
              </div>
              <AnimatePresence initial={false}>
                {revisedOpen && (
                  <motion.p
                    key="revised-prompt"
                    initial={{
                      opacity: 0,
                      transform: "translateY(-4px)",
                    }}
                    animate={{
                      opacity: 1,
                      transform: "translateY(0)",
                    }}
                    exit={{
                      opacity: 0,
                      transform: "translateY(-4px)",
                    }}
                    transition={{
                      duration: DURATION.quick,
                      ease: EASE.develop,
                    }}
                    className={cn(
                      "whitespace-pre-wrap break-words px-2 pt-1 text-sm leading-relaxed",
                      style.text,
                    )}
                  >
                    {revisedPrompt}
                  </motion.p>
                )}
              </AnimatePresence>
            </div>
          )}
        </section>
      )}

      {metadataSections.map((section) => (
        <section
          key={section.title}
          className={cn("rounded-[var(--radius-card)] border p-3", style.nested)}
        >
          <h3
            className={cn(
              "mb-2 font-mono text-[11px] uppercase tracking-wide",
              style.heading,
            )}
          >
            {section.title}
          </h3>
          <div className={cn("grid grid-cols-1 gap-2 font-mono text-xs", style.muted)}>
            {section.rows.map((row) => (
              <ParamRow
                key={`${section.title}-${row.label}`}
                row={row}
                tone={tone}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function ParamRow({
  row,
  tone,
}: {
  row: LightboxMetadataRow;
  tone: LightboxDetailsTone;
}) {
  const style = TONE[tone];
  return (
    <div className="grid min-w-0 grid-cols-[5.5rem_minmax(0,1fr)] items-baseline gap-2.5 py-0.5">
      <span
        className={cn(
          "text-[10px] font-medium uppercase tracking-wider",
          style.heading,
        )}
      >
        {row.label}
      </span>
      <span
        className={cn(
          "flex min-w-0 flex-wrap items-center gap-1.5 break-words text-[13px]",
          style.text,
        )}
      >
        <span className="min-w-0 break-words">{row.value}</span>
        {row.badge ? (
          <span
            className={cn(
              "shrink-0 rounded-full border px-1.5 py-0.5 font-sans text-[10px] font-medium",
              style.badge,
            )}
          >
            {row.badge}
          </span>
        ) : null}
      </span>
    </div>
  );
}
