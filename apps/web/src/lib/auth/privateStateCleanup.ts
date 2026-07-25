import {
  activatePrivateCanvasPersistence,
  clearPrivateCanvasPersistence,
} from "#canvas-persistence";

export function clearPrivateClientState(): Promise<void> {
  return clearPrivateCanvasPersistence();
}

export function activatePrivateClientState(userId: string): Promise<void> {
  return activatePrivateCanvasPersistence(userId);
}
