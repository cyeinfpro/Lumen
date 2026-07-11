import { z } from "zod";
import type {
  AspectRatio,
  AttachmentImage,
  GeneratedImage,
  Intent,
  RecommendedErrorAction,
  StructuredAttachment,
} from "../../lib/types";

type PayloadWarning = (
  message: string,
  context?: {
    code?: string;
    scope?: string;
    extra?: Record<string, unknown>;
  },
) => void;

const ASPECT_RATIOS = new Set<AspectRatio>([
  "1:1",
  "16:9",
  "9:16",
  "21:9",
  "9:21",
  "10:7",
  "7:10",
  "4:5",
  "3:4",
  "4:3",
  "3:2",
  "2:3",
]);

const ATTACHMENT_ROLES = new Set<StructuredAttachment["role"]>([
  "reference",
  "subject",
  "product",
  "style",
  "edit_target",
  "ask_target",
  "background",
  "mask",
  "other",
]);

const SsePayloadSchema = z.object({}).catchall(z.unknown());

export function coerceAspectRatio(
  value: unknown,
  fallback: AspectRatio,
): AspectRatio {
  return typeof value === "string" && ASPECT_RATIOS.has(value as AspectRatio)
    ? (value as AspectRatio)
    : fallback;
}

export function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

export function optionalString(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

export function stringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function optionalRecord(
  value: unknown,
  warn?: PayloadWarning,
): Record<string, unknown> | undefined {
  if (Array.isArray(value)) {
    warn?.("optional record payload dropped an array", {
      scope: "chat",
      code: "optional_record_array",
      extra: { length: value.length },
    });
    return undefined;
  }
  if (!value || typeof value !== "object") return undefined;
  return value as Record<string, unknown>;
}

export function firstOptionalRecord(
  first: unknown,
  second: unknown,
  warn?: PayloadWarning,
): Record<string, unknown> | undefined {
  return optionalRecord(first, warn) ?? optionalRecord(second, warn);
}

export function ssePayloadRecord(
  eventName: string,
  data: unknown,
  warn?: PayloadWarning,
): Record<string, unknown> | null {
  const parsed = SsePayloadSchema.safeParse(data);
  if (parsed.success) return parsed.data;
  warn?.("dropped SSE event with invalid payload", {
    scope: "chat-sse",
    extra: {
      event: eventName,
      payloadType: Array.isArray(data) ? "array" : typeof data,
      validation: "zod",
    },
  });
  return null;
}

export function optionalRecordArray(
  value: unknown,
): Array<Record<string, unknown>> | undefined {
  if (!Array.isArray(value)) return undefined;
  const records = value.filter(
    (item): item is Record<string, unknown> =>
      Boolean(item) && typeof item === "object" && !Array.isArray(item),
  );
  return records.length > 0 ? records : undefined;
}

export function recordString(
  record: Record<string, unknown>,
  key: string,
): string | undefined {
  const value = record[key];
  return typeof value === "string" && value ? value : undefined;
}

export function recordBoolean(
  record: Record<string, unknown>,
  key: string,
): boolean | undefined {
  const value = record[key];
  return typeof value === "boolean" ? value : undefined;
}

export function recordNullableString(
  record: Record<string, unknown>,
  key: string,
): string | null | undefined {
  const value = record[key];
  if (value === null) return null;
  return typeof value === "string" ? value : undefined;
}

export function recommendedActionsFromUnknown(
  value: unknown,
): RecommendedErrorAction[] | undefined {
  const items = optionalRecordArray(value);
  if (!items) return undefined;
  const actions: RecommendedErrorAction[] = [];
  for (const item of items) {
    const id = recordString(item, "id");
    const label = recordString(item, "label");
    if (!id || !label) continue;
    const action: RecommendedErrorAction = { id, label };
    const kind = recordString(item, "kind");
    if (kind) action.kind = kind;
    const href = recordNullableString(item, "href");
    if (href) action.href = href;
    actions.push(action);
  }
  return actions.length > 0 ? actions : undefined;
}

function defaultAttachmentRole(
  intent: Intent,
  index: number,
  hasMask: boolean,
): StructuredAttachment["role"] {
  if (intent === "vision_qa") return "ask_target";
  if (hasMask && index === 0) return "edit_target";
  return "reference";
}

function attachmentRole(
  value: unknown,
): StructuredAttachment["role"] | undefined {
  return typeof value === "string" &&
    ATTACHMENT_ROLES.has(value as StructuredAttachment["role"])
    ? (value as StructuredAttachment["role"])
    : undefined;
}

export function structuredAttachmentsFromComposer(
  attachments: AttachmentImage[],
  intent: Intent,
  hasMask: boolean,
): StructuredAttachment[] {
  return attachments.map((attachment, index) => {
    const role =
      attachmentRole(attachment.role) ??
      defaultAttachmentRole(intent, index, hasMask);
    return {
      image_id: attachment.source_image_id ?? attachment.id,
      role,
      ...(attachment.label ? { label: attachment.label } : {}),
      ...(typeof attachment.weight === "number"
        ? { weight: attachment.weight }
        : {}),
    };
  });
}

export function structuredAttachmentsFromUnknown(
  value: unknown,
): StructuredAttachment[] | undefined {
  const records = optionalRecordArray(value);
  if (!records) return undefined;
  const attachments = records
    .map((record) => {
      const imageId = recordString(record, "image_id");
      if (!imageId) return null;
      const label = recordString(record, "label");
      return {
        image_id: imageId,
        role: attachmentRole(record.role) ?? "reference",
        ...(label ? { label } : {}),
        ...(typeof record.weight === "number" ? { weight: record.weight } : {}),
      } satisfies StructuredAttachment;
    })
    .filter((item): item is StructuredAttachment => item !== null);
  return attachments.length > 0 ? attachments : undefined;
}

export function parseSizeString(
  value: unknown,
): { width: number; height: number } {
  if (typeof value !== "string") return { width: 0, height: 0 };
  const match = value.match(/^(\d+)x(\d+)$/);
  if (!match) return { width: 0, height: 0 };
  return { width: Number(match[1]), height: Number(match[2]) };
}

export function billingMetaFromPayload(
  payload: {
    is_dual_race_bonus?: unknown;
    billing_free?: unknown;
    billing_label?: unknown;
    billing_exempt_reason?: unknown;
  },
  metadata?: Record<string, unknown> | null,
): Pick<
  GeneratedImage,
  | "is_dual_race_bonus"
  | "billing_free"
  | "billing_label"
  | "billing_exempt_reason"
> {
  const isDualRaceBonus =
    payload.is_dual_race_bonus === true ||
    metadata?.is_dual_race_bonus === true;
  const billingLabel =
    optionalString(payload.billing_label) ??
    optionalString(metadata?.billing_label);
  const billingFree =
    payload.billing_free === true ||
    metadata?.billing_free === true ||
    isDualRaceBonus ||
    billingLabel === "free";
  return {
    is_dual_race_bonus: isDualRaceBonus || undefined,
    billing_free: billingFree || undefined,
    billing_label: billingLabel ?? (billingFree ? "free" : undefined),
    billing_exempt_reason:
      optionalString(payload.billing_exempt_reason) ??
      optionalString(metadata?.billing_exempt_reason),
  };
}

export function isoToMs(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}
