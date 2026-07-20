import { apiFetch, apiFetchNoContent } from "./http";
import type { NoContent } from "./http";
import type { SessionOut } from "../types";

// ——— Me: sessions ———

export function listMySessions(): Promise<{ items: SessionOut[] }> {
  return apiFetch<{ items: SessionOut[] }>("/me/sessions");
}

export function revokeMySession(id: string): Promise<NoContent> {
  return apiFetchNoContent(`/me/sessions/${id}`, { method: "DELETE" });
}

export function deleteMyAccount(): Promise<NoContent> {
  return apiFetchNoContent("/me", { method: "DELETE" });
}
