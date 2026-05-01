"use client";

export async function shareOrCopyLink(
  url: string,
  title = "Lumen 分享",
): Promise<"shared" | "copied" | "prompted" | "cancelled"> {
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

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(url);
      return "copied";
    } catch {
      // Fall through to the manual prompt fallback.
    }
  }

  window.prompt("复制分享链接", url);
  return "prompted";
}
