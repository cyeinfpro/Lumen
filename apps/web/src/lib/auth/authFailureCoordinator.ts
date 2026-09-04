import { requestSessionInvalidation } from "../runtimeResilience";
import { isPublicPath } from "./publicPaths";
import { clearPrivateClientState } from "./privateStateCleanup";
import { notifyAuthSessionChanged } from "./sessionChangeBus";

let cleanupFlight: Promise<void> | null = null;

export function invalidateSessionClientState(): Promise<void> {
  cleanupFlight ??= clearPrivateClientState().finally(() => {
    cleanupFlight = null;
  });
  return cleanupFlight;
}

export function coordinateUnauthorized(): void {
  notifyAuthSessionChanged();
  void invalidateSessionClientState();
  if (typeof window === "undefined") return;
  if (isPublicPath(window.location.pathname)) return;
  requestSessionInvalidation("http_unauthorized");
}
