import {
  activatePrivateCanvasPersistence,
  clearPrivateCanvasPersistence,
} from "#canvas-persistence";
import {
  transitionPrivateIdentity,
  type PrivateIdentitySnapshot,
} from "@/lib/auth/privateIdentityEpoch";
import { semanticPostIdempotency } from "@/lib/api/semanticIdempotency";
import { CLOSE_EVENT } from "@/lib/lightbox/types";
import { useInpaintStore } from "@/store/useInpaintStore";
import { useUiStore } from "@/store/useUiStore";

function resetPrivateSurfaces(identity: PrivateIdentitySnapshot): void {
  useInpaintStore.getState().resetForIdentity(identity);
  useUiStore.getState().resetPrivateUiForIdentity(identity);
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(CLOSE_EVENT));
  }
}

export function clearPrivateClientState(): Promise<void> {
  const identity = transitionPrivateIdentity(null);
  const semanticActivation = semanticPostIdempotency.activateIdentity(null);
  resetPrivateSurfaces(identity);
  return Promise.all([
    semanticActivation,
    clearPrivateCanvasPersistence(),
  ]).then(() => undefined);
}

export function activatePrivateClientState(userId: string): Promise<void> {
  const identity = transitionPrivateIdentity(userId);
  const semanticActivation = semanticPostIdempotency.activateIdentity(userId);
  if (identity.changed) {
    resetPrivateSurfaces(identity);
  }
  return Promise.all([
    semanticActivation,
    activatePrivateCanvasPersistence(userId),
  ]).then(() => undefined);
}
