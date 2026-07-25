export async function readResponseData(res: Response): Promise<unknown> {
  if (res.status === 204) return undefined;
  const contentType = res.headers.get("content-type") ?? "";
  return contentType.includes("application/json")
    ? await res.json().catch(() => null)
    : await res.text().catch(() => null);
}

export function sessionCookieSecureSignal(
  res: Response,
  data: unknown,
): boolean | null {
  const header = res.headers.get("x-lumen-session-cookie-secure")?.trim();
  if (header === "1" || header === "true") return true;
  if (header === "0" || header === "false") return false;
  if (!data || typeof data !== "object") return null;
  const direct = (data as { session_cookie_secure?: unknown })
    .session_cookie_secure;
  if (typeof direct === "boolean") return direct;
  const detail = (data as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object") return null;
  const nested = (detail as { session_cookie_secure?: unknown })
    .session_cookie_secure;
  return typeof nested === "boolean" ? nested : null;
}
