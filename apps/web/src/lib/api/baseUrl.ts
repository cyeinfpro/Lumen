export function computeApiBase(): string {
  const explicit = process.env.NEXT_PUBLIC_API_BASE?.trim();
  return explicit ? explicit.replace(/\/$/, "") : "/api";
}

export const API_BASE = computeApiBase();

export function apiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${API_BASE.replace(/\/$/, "")}${suffix}`;
}
