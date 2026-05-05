"use client";

export async function shareOrCopyLink(
  url: string,
  title = "Lumen 分享",
): Promise<"shared" | "copied" | "failed" | "cancelled"> {
  if (typeof navigator !== "undefined" && typeof navigator.share === "function") {
    try {
      await navigator.share({ title, url });
      return "shared";
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        return "cancelled";
      }
    }
  }

  try {
    await writeClipboardText(url);
    return "copied";
  } catch {
    return "failed";
  }
}

// navigator.clipboard 在非 secure context（http）不可用，退化到 execCommand。
async function writeClipboardText(text: string): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  if (typeof document === "undefined") {
    throw new Error("clipboard unavailable");
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    const ok = document.execCommand("copy");
    if (!ok) throw new Error("copy command failed");
  } finally {
    document.body.removeChild(textarea);
  }
}
