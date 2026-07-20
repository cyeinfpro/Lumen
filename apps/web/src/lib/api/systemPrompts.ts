import { apiFetch, apiFetchNoContent } from "./http";
import type { NoContent } from "./http";

// —— 系统提示词库 ——

export interface SystemPrompt {
  id: string;
  name: string;
  content: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface SystemPromptListResponse {
  items: SystemPrompt[];
  default_id?: string | null;
}

export interface CreateSystemPromptIn {
  name: string;
  content: string;
  make_default?: boolean;
}

export interface PatchSystemPromptIn {
  name?: string;
  content?: string;
  make_default?: boolean;
}

export function listSystemPrompts(): Promise<SystemPromptListResponse> {
  return apiFetch<SystemPromptListResponse>("/system-prompts");
}

export function createSystemPrompt(
  body: CreateSystemPromptIn,
): Promise<SystemPrompt> {
  return apiFetch<SystemPrompt>("/system-prompts", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function patchSystemPrompt(
  id: string,
  body: PatchSystemPromptIn,
): Promise<SystemPrompt> {
  return apiFetch<SystemPrompt>(`/system-prompts/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteSystemPrompt(id: string): Promise<NoContent> {
  return apiFetchNoContent(`/system-prompts/${id}`, { method: "DELETE" });
}

export function setDefaultSystemPrompt(id: string): Promise<SystemPrompt> {
  return apiFetch<SystemPrompt>(`/system-prompts/${id}/default`, {
    method: "POST",
  });
}
