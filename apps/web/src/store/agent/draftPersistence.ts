import type {
  AgentDraft,
  AgentDraftAttachment,
  AgentDraftFile,
} from "@/features/agent/model/contracts";
import {
  AGENT_MAX_FILE_BYTES,
  AGENT_MAX_FILES,
  AGENT_MAX_REFERENCES,
  AGENT_MAX_TOTAL_FILE_BYTES,
  createAgentDraft,
} from "@/features/agent/model/contracts";

const STORAGE_KEY = "lumen.agent.drafts.v1";
const REASONING_EFFORTS = new Set([
  "auto",
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
]);

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
  files?: AgentDraftFile[];
  allowImage: boolean;
  allowWebSearch?: boolean;
  allowFileTools?: boolean;
  imageDefaults: AgentDraft["imageDefaults"];
  reasoningEffort?: AgentDraft["reasoningEffort"];
}

interface PersistedEnvelope {
  version: 1 | 2 | 3;
  ownerUserId: string;
  drafts: Record<string, PersistedDraft>;
}

function restoredReasoningEffort(
  version: PersistedEnvelope["version"],
  effort: AgentDraft["reasoningEffort"],
): AgentDraft["reasoningEffort"] {
  if (typeof effort !== "string" || !REASONING_EFFORTS.has(effort)) {
    return "auto";
  }
  return version === 1 && effort === "max" ? "auto" : effort;
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
    files: draft.files.map((file) => ({ ...file })),
    allowImage: draft.allowImage,
    allowWebSearch: draft.allowWebSearch,
    allowFileTools: draft.allowFileTools,
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
    version: 3,
    ownerUserId,
    drafts: safeDrafts,
  } satisfies PersistedEnvelope);
}

function restoreAttachments(draft: PersistedDraft): AgentDraftAttachment[] {
  if (!Array.isArray(draft.attachments)) return [];
  return draft.attachments
    .filter(
      (item) =>
        item &&
        typeof item.imageId === "string" &&
        item.imageId.length > 0 &&
        typeof item.role === "string",
    )
    .slice(0, AGENT_MAX_REFERENCES)
    .map((item, index) => ({
      imageId: item.imageId,
      role: item.role,
      label: typeof item.label === "string" ? item.label : null,
      name: item.label || `参考图 ${index + 1}`,
      previewUrl: attachmentPreviewUrl(item.imageId),
    }));
}

function restoredFile(
  item: AgentDraftFile,
  totalFileBytes: number,
): AgentDraftFile | null {
  if (
    !item ||
    typeof item.name !== "string" ||
    !item.name ||
    typeof item.mimeType !== "string" ||
    typeof item.size !== "number" ||
    item.size < 0 ||
    item.size > AGENT_MAX_FILE_BYTES ||
    typeof item.content !== "string" ||
    item.content.includes("\u0000") ||
    totalFileBytes + item.size > AGENT_MAX_TOTAL_FILE_BYTES
  ) {
    return null;
  }
  return {
    name: item.name.slice(0, 128),
    mimeType: item.mimeType.slice(0, 96),
    size: item.size,
    content: item.content.slice(0, 200_000),
  };
}

function restoreFiles(draft: PersistedDraft): AgentDraftFile[] {
  if (!Array.isArray(draft.files)) return [];
  const files: AgentDraftFile[] = [];
  let totalFileBytes = 0;
  for (const item of draft.files.slice(0, AGENT_MAX_FILES)) {
    const restored = restoredFile(item, totalFileBytes);
    if (!restored) continue;
    files.push(restored);
    totalFileBytes += restored.size;
  }
  return files;
}

function restoreDraft(
  draft: PersistedDraft,
  version: PersistedEnvelope["version"],
): AgentDraft {
  return createAgentDraft({
    text: typeof draft.text === "string" ? draft.text : "",
    attachments: restoreAttachments(draft),
    files: restoreFiles(draft),
    allowImage: draft.allowImage !== false,
    allowWebSearch: draft.allowWebSearch === true,
    allowFileTools: draft.allowFileTools !== false,
    imageDefaults: draft.imageDefaults,
    reasoningEffort: restoredReasoningEffort(version, draft.reasoningEffort),
  });
}

function validEnvelope(
  parsed: Partial<PersistedEnvelope>,
  ownerUserId: string,
): parsed is PersistedEnvelope {
  return (
    (parsed.version === 1 || parsed.version === 2 || parsed.version === 3) &&
    parsed.ownerUserId === ownerUserId &&
    Boolean(parsed.drafts) &&
    typeof parsed.drafts === "object"
  );
}

export function deserializeAgentDrafts(
  raw: string | null,
  ownerUserId: string,
): Record<string, AgentDraft> {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw) as Partial<PersistedEnvelope>;
    if (!validEnvelope(parsed, ownerUserId)) return {};
    const drafts: Record<string, AgentDraft> = {};
    for (const [key, value] of Object.entries(parsed.drafts)) {
      if (!value || typeof value !== "object") continue;
      drafts[key] = restoreDraft(value, parsed.version);
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
