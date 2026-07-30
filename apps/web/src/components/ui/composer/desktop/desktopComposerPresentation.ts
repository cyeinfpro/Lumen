import { cn } from "@/lib/utils";

export function parseDesktopComposerSlash(text: string): {
  stripped: string;
  force?: "chat" | "image";
} {
  const match = /^\s*\/(ask|image)(\s+|$)/i.exec(text);
  if (!match) return { stripped: text };
  const command = match[1].toLowerCase();
  return {
    stripped: text.slice(match[0].length).trim(),
    force: command === "ask" ? "chat" : "image",
  };
}

export function desktopComposerFrameClass(
  expanded: boolean,
  isDragActive: boolean,
): string {
  return cn(
    "fixed bottom-4 -translate-x-1/2",
    "max-w-[var(--content-composer)]",
    "overflow-visible",
    "rounded-[var(--radius-panel)]",
    "bg-[var(--bg-1)]/97",
    "border transition-[border-color,box-shadow] duration-[var(--dur-normal)]",
    isDragActive
      ? "border-[var(--accent)]"
      : "border-[var(--border-subtle)] focus-within:border-[var(--accent-border)]",
    expanded ? "shadow-[var(--shadow-2)]" : "shadow-[var(--shadow-1)]",
  );
}

export function desktopComposerFrameWidth(): string {
  return "min(var(--content-composer), calc(100vw - var(--studio-sidebar-offset, 0px) - 40px))";
}
