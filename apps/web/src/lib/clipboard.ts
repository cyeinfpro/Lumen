"use client";

function preserveSelection(active: HTMLElement | null): () => void {
  const selection = document.getSelection();
  const ranges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) =>
        selection.getRangeAt(index).cloneRange(),
      )
    : [];
  const field = active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement
    ? active
    : null;
  const start = field?.selectionStart ?? null;
  const end = field?.selectionEnd ?? null;
  const direction = field?.selectionDirection ?? "none";

  return () => {
    if (active?.isConnected) active.focus({ preventScroll: true });
    if (field?.isConnected && start !== null && end !== null) {
      field.setSelectionRange(start, end, direction);
    } else if (selection && ranges.length) {
      selection.removeAllRanges();
      for (const range of ranges) {
        if (range.commonAncestorContainer.isConnected) selection.addRange(range);
      }
    }
  };
}

function copyWithSelection(text: string): void {
  if (typeof document === "undefined") throw new Error("clipboard unavailable");
  const active = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
  const restoreSelection = preserveSelection(active);
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.tabIndex = -1;
  // Keep the temporary field inside the active modal: its siblings may be inert.
  const root = active?.closest('[role="dialog"], dialog') ?? document.body;
  Object.assign(textarea.style, {
    position: "fixed", left: "0", top: "0", width: "1px", height: "1px",
    opacity: "0", pointerEvents: "none", fontSize: "16px",
  });
  root.appendChild(textarea);
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    if (!document.execCommand("copy")) throw new Error("copy command failed");
  } finally {
    textarea.remove();
    restoreSelection();
  }
}

export async function copyTextToClipboard(text: string): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // An available API can still reject (permissions, browser policy, iframe).
      // Only report success if the fallback actually completes the copy.
    }
  }
  copyWithSelection(text);
}

export async function tryCopyTextToClipboard(text: string): Promise<boolean> {
  try {
    await copyTextToClipboard(text);
    return true;
  } catch {
    return false;
  }
}
