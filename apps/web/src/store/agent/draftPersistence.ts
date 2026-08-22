import type {
  AgentDraft,
  AgentDraftAttachment,
} from "@/features/agent/model/contracts";
import { createAgentDraft } from "@/features/agent/model/contracts";

const STORAGE_KEY = "lumen.agent.drafts.v1";

function ownerStorageKey(ownerUserId: string): string {
  return `${STORAGE_KEY}:${encodeURIComponent(ownerUserId)}`;
}

interface PersistedAttachment {
  imageId: string;
  role: AgentDraftAttachment["role"];
  label: string | null;
}

interface PersistedDraft {
  text: string;
  attachments: PersistedAttachment[];
  allowImage: boolean;
  imageDefaults: AgentDraft["imageDefaults"];
  reasoningEffort?: AgentDraft["reasoningEffort"];
}

interface PersistedEnvelope {
  version: 1;
  ownerUserId: string;
  drafts: Record<string, PersistedDraft>;
}

function attachmentPreviewUrl(imageId: string): string {
  return `/api/images/${encodeURIComponent(imageId)}/variants/thumb256`;
}

function persistedDraft(draft: AgentDraft): PersistedDraft {
  return {
    text: draft.text,
    attachments: draft.attachments.map((attachment) => ({
      imageId: attachment.imageId,
      role: attachment.role,
      label: attachment.label,
    })),
    allowImage: draft.allowImage,
    imageDefaults: { ...draft.imageDefaults },
    reasoningEffort: draft.reasoningEffort,
  };
}

export function serializeAgentDrafts(
  ownerUserId: string,
  drafts: Record<string, AgentDraft>,
): string {
  const safeDrafts = Object.fromEntries(
    Object.entries(drafts).map(([key, draft]) => [key, persistedDraft(draft)]),
  );
  return JSON.stringify({
    version: 1,
    ownerUserId,
    drafts: safeDrafts,
  } satisfies PersistedEnvelope);
}

export function deserializeAgentDrafts(
  raw: string | null,
  ownerUserId: string,
): Record<string, AgentDraft> {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as Partial<PersistedEnvelope>;
    if (
      parsed.version !== 1 ||
      parsed.ownerUserId !== ownerUserId ||
      !parsed.drafts ||
      typeof parsed.drafts !== "object"
    ) {
      return {};
    }
    const drafts: Record<string, AgentDraft> = {};
    for (const [key, value] of Object.entries(parsed.drafts)) {
      if (!value || typeof value !== "object") continue;
      const draft = value as PersistedDraft;
      const attachments = Array.isArray(draft.attachments)
        ? draft.attachments
            .filter(
              (item) =>
                item &&
                typeof item.imageId === "string" &&
                item.imageId.length > 0 &&
                typeof item.role === "string",
            )
            .slice(0, 4)
            .map((item, index) => ({
              imageId: item.imageId,
              role: item.role,
              label: typeof item.label === "string" ? item.label : null,
              name: item.label || `参考图 ${index + 1}`,
              previewUrl: attachmentPreviewUrl(item.imageId),
            }))
        : [];
      drafts[key] = createAgentDraft({
        text: typeof draft.text === "string" ? draft.text : "",
        attachments,
        allowImage: draft.allowImage !== false,
        imageDefaults: draft.imageDefaults,
        reasoningEffort: draft.reasoningEffort,
      });
    }
    return drafts;
  } catch {
    return {};
  }
}

export function loadAgentDrafts(ownerUserId: string): Record<string, AgentDraft> {
  if (typeof localStorage === "undefined") return {};
  return deserializeAgentDrafts(
    localStorage.getItem(ownerStorageKey(ownerUserId)),
    ownerUserId,
  );
}

export function saveAgentDrafts(
  ownerUserId: string | null,
  drafts: Record<string, AgentDraft>,
): void {
  if (!ownerUserId || typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(
      ownerStorageKey(ownerUserId),
      serializeAgentDrafts(ownerUserId, drafts),
    );
  } catch {
    // Draft persistence is best-effort; in-memory editing remains available.
  }
}

export function removeAgentDrafts(ownerUserId: string | null): void {
  if (!ownerUserId || typeof localStorage === "undefined") return;
  try {
    localStorage.removeItem(ownerStorageKey(ownerUserId));
  } catch {
    // Account deletion remains server-authoritative if browser storage is blocked.
  }
}
