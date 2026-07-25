import { isPublicPath } from "./publicPaths";
import { replaceWithLogin } from "./navigation";
import { clearPrivateClientState } from "./privateStateCleanup";

let redirecting = false;
let cleanupFlight: Promise<void> | null = null;

export function invalidateSessionClientState(): Promise<void> {
  cleanupFlight ??= clearPrivateClientState().finally(() => {
    cleanupFlight = null;
  });
  return cleanupFlight;
}

export function coordinateUnauthorized(): void {
  const cleanup = invalidateSessionClientState();
  if (typeof window === "undefined" || redirecting) return;
  if (isPublicPath(window.location.pathname)) return;
  redirecting = true;
  void cleanup.finally(() => {
    try {
      replaceWithLogin();
    } catch {
      redirecting = false;
    }
  });
}
