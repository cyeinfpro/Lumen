export interface PromptImageMentionAttachment {
  id: string;
}

export interface InsertImageMentionResult {
  text: string;
  selectionStart: number;
  selectionEnd: number;
}

const IMAGE_MENTION_RE = /@图([1-9]\d*)/g;
const REMOVED_IMAGE_MENTION_RE = /\u200b@已移除/g;
const REMOVED_IMAGE_MENTION = "\u200b@已移除";
const REQUEST_REMOVED_IMAGE_MENTION = "[removed image]";

function clampSelection(value: number | undefined, length: number): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return length;
  return Math.max(0, Math.min(length, Math.trunc(value)));
}

function normalizeImageNumber(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.trunc(value));
}

function needsLeadingSpace(before: string): boolean {
  if (!before) return false;
  return !/[\s([{\u300a\u300c\u300e\uff08\u3010]$/.test(before);
}

function needsTrailingSpace(after: string): boolean {
  if (!after) return false;
  return !/^[\s,.;:!?，。；：！？、)\]}\u300b\u300d\u300f\uff09\u3011]/.test(
    after,
  );
}

function imageMentionLabel(imageNumber: number): string {
  return `@图${normalizeImageNumber(imageNumber)}`;
}

export function insertImageMentionToken(
  text: string,
  imageNumber: number,
  selectionStart?: number,
  selectionEnd?: number,
): InsertImageMentionResult {
  const start = clampSelection(selectionStart, text.length);
  const end = clampSelection(selectionEnd, text.length);
  const from = Math.min(start, end);
  const to = Math.max(start, end);
  const before = text.slice(0, from);
  const after = text.slice(to);
  const token = imageMentionLabel(imageNumber);
  const insert = [
    needsLeadingSpace(before) ? " " : "",
    token,
    needsTrailingSpace(after) ? " " : "",
  ].join("");
  const nextText = `${before}${insert}${after}`;
  const caret = before.length + insert.length;
  return {
    text: nextText,
    selectionStart: caret,
    selectionEnd: caret,
  };
}

export function remapPromptImageMentions(
  text: string,
  previousAttachments: PromptImageMentionAttachment[],
  nextAttachments: PromptImageMentionAttachment[],
): string {
  if (!text.includes("@图") || previousAttachments.length === 0) return text;
  const nextIndexById = new Map(
    nextAttachments.map((attachment, index) => [attachment.id, index + 1]),
  );
  return text.replace(IMAGE_MENTION_RE, (match, rawIndex: string) => {
    const previousIndex = Number.parseInt(rawIndex, 10) - 1;
    if (
      previousIndex < 0 ||
      previousIndex >= previousAttachments.length ||
      !Number.isSafeInteger(previousIndex)
    ) {
      return match;
    }
    const previousAttachment = previousAttachments[previousIndex];
    if (!previousAttachment) return match;
    const nextIndex = nextIndexById.get(previousAttachment.id);
    return nextIndex == null ? REMOVED_IMAGE_MENTION : imageMentionLabel(nextIndex);
  });
}

export function findInvalidImageMentionLabels(
  text: string,
  attachmentCount: number,
): string[] {
  if (!text.includes("@图")) return [];
  const invalid: string[] = [];
  const seen = new Set<string>();
  for (const match of text.matchAll(IMAGE_MENTION_RE)) {
    const label = match[0];
    if (seen.has(label)) continue;
    const index = Number.parseInt(match[1] ?? "", 10);
    if (
      !Number.isSafeInteger(index) ||
      index < 1 ||
      index > attachmentCount
    ) {
      invalid.push(label);
      seen.add(label);
    }
  }
  return invalid;
}

export function serializePromptImageMentionsForRequest(
  text: string,
  attachments: PromptImageMentionAttachment[],
): string {
  if (!text.includes("@图") && !text.includes(REMOVED_IMAGE_MENTION)) {
    return text;
  }
  const withRemovedMentions = text.replace(
    REMOVED_IMAGE_MENTION_RE,
    REQUEST_REMOVED_IMAGE_MENTION,
  );
  if (attachments.length === 0) return withRemovedMentions;
  return withRemovedMentions.replace(
    IMAGE_MENTION_RE,
    (_match, rawIndex: string) => {
      const index = Number.parseInt(rawIndex, 10);
      if (
        !Number.isSafeInteger(index) ||
        index < 1 ||
        index > attachments.length
      ) {
        return REQUEST_REMOVED_IMAGE_MENTION;
      }
      return `[image ${index}]`;
    },
  );
}
