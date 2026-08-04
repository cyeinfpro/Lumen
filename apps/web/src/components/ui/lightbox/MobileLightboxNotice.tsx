import { motion } from "framer-motion";
import { SPRING } from "@/lib/motion";
import { cn } from "@/lib/utils";
import type { ActionNotice } from "./MobileLightboxViewTypes";

export function LightboxNotice({
  actionNotice,
  boundaryHint,
}: {
  actionNotice: ActionNotice;
  boundaryHint: "first" | "last" | null;
}) {
  if (!actionNotice && !boundaryHint) return null;
  const text =
    boundaryHint === "first"
      ? "已经是第一张"
      : boundaryHint === "last"
        ? "已经是最后一张"
        : actionNotice?.text;
  const isError = actionNotice?.kind === "error";
  return (
    <motion.div
      key={actionNotice?.text ?? boundaryHint}
      initial={{ opacity: 0, y: -8, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -8, scale: 0.98 }}
      transition={SPRING.snap}
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
      className={cn(
        "pointer-events-none absolute left-1/2 top-[calc(env(safe-area-inset-top)+4.25rem)]",
        "type-caption -translate-x-1/2 rounded-full border px-3 py-1.5",
        "bg-[var(--media-control-bg)] text-[var(--media-control-fg)] shadow-[var(--shadow-2)]",
        isError ? "border-danger-border" : "border-[var(--border)]",
      )}
    >
      {text}
    </motion.div>
  );
}
