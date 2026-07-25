import { isPublicPath } from "./publicPaths";

export function safeAuthNextPath(raw: string, origin?: string): string {
  const trimmed = typeof raw === "string" ? raw.trim() : "";
  if (!trimmed || trimmed.startsWith("//")) return "/";
  try {
    const base =
      origin ??
      (typeof window !== "undefined"
        ? window.location.origin
        : "http://localhost");
    const parsed = new URL(trimmed, base);
    if (parsed.origin !== new URL(base).origin) return "/";
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "/";
    if (!parsed.pathname.startsWith("/") || isPublicPath(parsed.pathname)) {
      return "/";
    }
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return "/";
  }
}

export function currentLoginPath(): string {
  if (typeof window === "undefined") return "/login";
  const next = safeAuthNextPath(
    `${window.location.pathname}${window.location.search}${window.location.hash}`,
    window.location.origin,
  );
  return `/login?next=${encodeURIComponent(next)}`;
}

export function replaceWithLogin(): void {
  if (typeof window === "undefined" || isPublicPath(window.location.pathname)) {
    return;
  }
  window.location.replace(currentLoginPath());
}
