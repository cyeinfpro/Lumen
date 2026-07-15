import type {
  VideoAction,
  VideoModelOptionOut,
  VideoReferenceMediaIn,
} from "../../lib/types";

export type ReferenceKind = VideoReferenceMediaIn["kind"];
export type ReferenceLimits = Record<ReferenceKind, number>;

type ReferenceIdentity = Pick<VideoReferenceMediaIn, "kind" | "ref_id">;
type LabeledReference = ReferenceIdentity & { label: string };
type ReferencePayloadSource = VideoReferenceMediaIn & {
  label: string;
  ref_id: string;
};
export type VolcanoAssetReferenceCandidate = {
  id: string;
  name: string;
  asset_type: "Image" | "Video";
  url?: string | null;
};
type DraftReference = ReferencePayloadSource & {
  _key: string;
  display: string;
  previewUrl?: string | null;
};

const REFERENCE_REF_ID_RE = /^ref:(image|video|audio):([1-9][0-9]{0,2})$/;
export const REFERENCE_KINDS: ReferenceKind[] = ["image", "video", "audio"];
export const DEFAULT_REFERENCE_LIMITS: ReferenceLimits = {
  image: 9,
  video: 3,
  audio: 1,
};
const NEWAPI_REFERENCE_LIMITS: ReferenceLimits = {
  image: 4,
  video: 3,
  audio: 1,
};
const CHINESE_DIGITS: Record<number, string> = {
  1: "一",
  2: "二",
  3: "三",
  4: "四",
  5: "五",
  6: "六",
  7: "七",
  8: "八",
  9: "九",
};

export function normalizeAssetUrl(value: string): string {
  const raw = value
    .trim()
    .replace(/^["'`“”‘’]+|["'`“”‘’]+$/g, "")
    .trim();
  if (!raw) return "";
  const stripped = raw
    .replace(/^asset\s*:\s*\/\s*\//i, "")
    .replace(/^[/\\]+/, "")
    .trim();
  const assetId = stripped.toLowerCase();
  return /^asset-[a-z0-9][a-z0-9_-]*$/.test(assetId)
    ? `asset://${assetId}`
    : "";
}

export function assetIdFromReferenceUrl(
  value: string | null | undefined,
): string | null {
  const normalized = normalizeAssetUrl(value ?? "");
  return normalized ? normalized.slice("asset://".length) : null;
}

export function appendVolcanoAssetReferences(
  refs: readonly DraftReference[],
  assets: readonly VolcanoAssetReferenceCandidate[],
  limits: ReferenceLimits,
  keyFactory: () => string,
): { references: DraftReference[]; added: number } {
  let references = [...refs];
  let added = 0;
  for (const asset of assets) {
    const kind: ReferenceKind =
      asset.asset_type === "Image" ? "image" : "video";
    const url = normalizeAssetUrl(asset.id);
    const assetId = assetIdFromReferenceUrl(url);
    if (
      !url ||
      !assetId ||
      references.some(
        (item) => assetIdFromReferenceUrl(item.url) === assetId,
      ) ||
      references.filter((item) => item.kind === kind).length >= limits[kind]
    ) {
      continue;
    }
    const identity = nextReferenceIdentity(kind, references);
    references = [
      ...references,
      {
        _key: keyFactory(),
        kind,
        url,
        label: identity.label,
        ref_id: identity.refId,
        display: asset.name || url,
        previewUrl:
          asset.url && !/^asset:\/\//i.test(asset.url)
            ? asset.url.trim() || null
            : null,
      },
    ];
    added += 1;
  }
  return { references, added };
}

export function isNewApiVideoModel(model: string): boolean {
  const value = model.trim().toLowerCase().replace(/[_.]/g, "-");
  return value === "video-ds-2-0" || value.startsWith("video-ds-2-0-");
}

export function referenceLimitsForModel(model: string): ReferenceLimits {
  return isNewApiVideoModel(model)
    ? NEWAPI_REFERENCE_LIMITS
    : DEFAULT_REFERENCE_LIMITS;
}

export function referenceLimitsForModelOption(
  option: VideoModelOptionOut | null | undefined,
  model: string,
): ReferenceLimits {
  const fallback = referenceLimitsForModel(model);
  const limits = option?.reference_media_limits;
  const undeclaredKindLimit = limits ? 0 : undefined;
  return {
    image: normalizeReferenceLimit(
      limits?.image,
      undeclaredKindLimit ?? fallback.image,
    ),
    video: normalizeReferenceLimit(
      limits?.video,
      undeclaredKindLimit ?? fallback.video,
    ),
    audio: normalizeReferenceLimit(
      limits?.audio,
      undeclaredKindLimit ?? fallback.audio,
    ),
  };
}

function normalizeReferenceLimit(
  value: number | null | undefined,
  fallback: number,
): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : fallback;
}

export function referenceRefId(kind: ReferenceKind, index: number): string {
  return `ref:${kind}:${index}`;
}

export function referenceRefIndex(
  refId: string | null | undefined,
  kind: ReferenceKind,
): number | null {
  const match = (refId ?? "").trim().toLowerCase().match(REFERENCE_REF_ID_RE);
  if (!match || match[1] !== kind) return null;
  const index = Number(match[2]);
  return Number.isInteger(index) && index > 0 ? index : null;
}

export function referenceKindNoun(kind: ReferenceKind): string {
  if (kind === "image") return "图片";
  if (kind === "audio") return "音频";
  return "视频";
}

function referenceKindShortNoun(kind: ReferenceKind): string {
  if (kind === "image") return "图";
  return referenceKindNoun(kind);
}

export function referenceLabel(kind: ReferenceKind, index: number): string {
  return `${referenceKindNoun(kind)} ${index}`;
}

export function referenceLimitMessage(
  kind: ReferenceKind,
  limit: number,
): string {
  const unit = kind === "image" ? "张" : "个";
  return `参考${referenceKindNoun(kind)}最多 ${limit} ${unit}`;
}

export function referenceCountsFor(
  refs: ReadonlyArray<Pick<VideoReferenceMediaIn, "kind">>,
): ReferenceLimits {
  return {
    image: refs.filter((item) => item.kind === "image").length,
    video: refs.filter((item) => item.kind === "video").length,
    audio: refs.filter((item) => item.kind === "audio").length,
  };
}

export function referenceLimitViolation(
  refs: ReadonlyArray<Pick<VideoReferenceMediaIn, "kind">>,
  limits: ReferenceLimits,
): string | null {
  const counts = referenceCountsFor(refs);
  for (const kind of REFERENCE_KINDS) {
    if (counts[kind] > limits[kind]) {
      return referenceLimitMessage(kind, limits[kind]);
    }
  }
  return null;
}

export function nextReferenceIdentity(
  kind: ReferenceKind,
  refs: ReadonlyArray<ReferenceIdentity>,
): { refId: string; label: string } {
  const maxIndex = refs.reduce((max, item) => {
    if (item.kind !== kind) return max;
    return Math.max(max, referenceRefIndex(item.ref_id, kind) ?? 0);
  }, 0);
  const index = maxIndex + 1;
  return {
    refId: referenceRefId(kind, index),
    label: referenceLabel(kind, index),
  };
}

export function referencePromptToken(
  item: ReferenceIdentity,
  fallbackIndex = 1,
): string {
  const rawRefId = item.ref_id?.trim().toLowerCase() ?? "";
  const index = referenceRefIndex(rawRefId, item.kind);
  return `[${index ? rawRefId : referenceRefId(item.kind, fallbackIndex)}]`;
}

export function referenceDisplayToken(
  item: ReferenceIdentity,
  fallbackIndex = 1,
): string {
  const rawRefId = item.ref_id?.trim().toLowerCase() ?? "";
  const index = referenceRefIndex(rawRefId, item.kind) ?? fallbackIndex;
  return `@${referenceKindNoun(item.kind)}${index}`;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function referenceDisplayAliases(item: LabeledReference): string[] {
  const index = referenceRefIndex(item.ref_id, item.kind);
  if (!index) return [];
  const noun = referenceKindNoun(item.kind);
  const shortNoun = referenceKindShortNoun(item.kind);
  return [
    referenceDisplayToken(item),
    `@${noun} ${index}`,
    `@${shortNoun}${index}`,
    `@${shortNoun} ${index}`,
  ];
}

function referenceRoleAliases(kind: ReferenceKind, index: number): string[] {
  if (kind === "video") {
    return [
      `视频素材 ${index}`,
      `视频素材${index}`,
      `参考视频 ${index}`,
      `参考视频${index}`,
      `动作参考 ${index}`,
      `动作参考${index}`,
      `运动参考 ${index}`,
      `运动参考${index}`,
    ];
  }
  if (kind === "audio") {
    return [
      `音频素材 ${index}`,
      `音频素材${index}`,
      `参考音频 ${index}`,
      `参考音频${index}`,
    ];
  }
  return [];
}

function numberedReferenceAliases(
  kind: ReferenceKind,
  index: number,
  noun: string,
  shortNoun: string,
): string[] {
  if (kind === "image") {
    return [`第${index}张${noun}`, `第${index}张${shortNoun}`];
  }
  if (kind === "video") {
    return [
      `第${index}个${noun}`,
      `第${index}段${noun}`,
      `第${index}段素材`,
      `第${index}个视频素材`,
    ];
  }
  return [
    `第${index}个${noun}`,
    `第${index}段${noun}`,
    `第${index}段音频素材`,
    `第${index}个音频素材`,
  ];
}

function chineseNumberedReferenceAliases(
  kind: ReferenceKind,
  zh: string | undefined,
  noun: string,
  shortNoun: string,
): string[] {
  if (!zh) return [];
  if (kind === "image") {
    return [`第${zh}张${noun}`, `第${zh}张${shortNoun}`];
  }
  if (kind === "video") {
    return [
      `第${zh}个${noun}`,
      `第${zh}段${noun}`,
      `第${zh}段素材`,
      `第${zh}个视频素材`,
    ];
  }
  return [
    `第${zh}个${noun}`,
    `第${zh}段${noun}`,
    `第${zh}段音频素材`,
    `第${zh}个音频素材`,
  ];
}

export function referenceMentionAliases(item: LabeledReference): string[] {
  const index = referenceRefIndex(item.ref_id, item.kind);
  if (!index) return [];
  const aliases = new Set<string>();
  const noun = referenceKindNoun(item.kind);
  const shortNoun = referenceKindShortNoun(item.kind);
  const zh = CHINESE_DIGITS[index];
  for (const alias of [
    item.label,
    `[${item.label}]`,
    `${noun} ${index}`,
    `${noun}${index}`,
    `${shortNoun}${index}`,
    ...referenceRoleAliases(item.kind, index),
    ...numberedReferenceAliases(item.kind, index, noun, shortNoun),
    ...chineseNumberedReferenceAliases(item.kind, zh, noun, shortNoun),
  ]) {
    const clean = alias.trim();
    if (clean) aliases.add(clean);
  }
  return Array.from(aliases);
}

function replaceReferenceDisplayMentionsWithAnchors(
  text: string,
  refs: readonly LabeledReference[],
): string {
  let next = text;
  for (const item of refs) {
    const token = referencePromptToken(item);
    for (const alias of referenceDisplayAliases(item)) {
      next = next.replace(new RegExp(escapeRegExp(alias), "g"), token);
    }
  }
  return next;
}

export function normalizePromptReferenceMentions(
  text: string,
  refs: readonly LabeledReference[],
): string {
  if (!text.trim() || refs.length === 0) return text;
  let next = text;
  for (const item of refs) {
    const token = referencePromptToken(item);
    if (next.includes(token)) continue;
    for (const alias of referenceMentionAliases(item)) {
      const pattern = new RegExp(escapeRegExp(alias), "i");
      if (!pattern.test(next)) continue;
      next = next.replace(pattern, (match) => `${match} ${token}`);
      break;
    }
  }

  for (const kind of ["image", "video"] as const) {
    const sameKindRefs = refs.filter((item) => item.kind === kind);
    if (sameKindRefs.length !== 1) continue;
    const item = sameKindRefs[0];
    const token = referencePromptToken(item);
    if (next.includes(token)) continue;
    const phrases =
      kind === "image"
        ? [
            "这张参考图",
            "这个参考图",
            "这张图片",
            "这个图片",
            "这张图",
            "这个图",
          ]
        : [
            "这段参考视频",
            "这个参考视频",
            "这段视频素材",
            "这个视频素材",
            "这段动作参考",
            "这个动作参考",
            "这段运动参考",
            "这个运动参考",
            "这段素材",
            "这个素材",
            "这段视频",
            "这个视频",
          ];
    for (const phrase of phrases) {
      const pattern = new RegExp(escapeRegExp(phrase), "i");
      if (!pattern.test(next)) continue;
      next = next.replace(pattern, (match) => `${match} ${token}`);
      break;
    }
  }
  return next;
}

export function serializePromptReferenceMentions(
  text: string,
  refs: readonly LabeledReference[],
): string {
  return normalizePromptReferenceMentions(
    replaceReferenceDisplayMentionsWithAnchors(text, refs),
    refs,
  );
}

export function displayPromptReferenceMentions(
  text: string,
  refs: readonly LabeledReference[],
): string {
  let next = text;
  for (const item of refs) {
    next = next.replace(
      new RegExp(escapeRegExp(referencePromptToken(item)), "g"),
      referenceDisplayToken(item),
    );
  }
  return next;
}

export function displayPromptEnhanceCandidates<T extends { prompt: string }>(
  candidates: T[],
  refs: readonly LabeledReference[],
): T[] {
  if (refs.length === 0) return candidates;
  return candidates.map((candidate) => ({
    ...candidate,
    prompt: displayPromptReferenceMentions(candidate.prompt, refs),
  }));
}

export function promptContainsReferenceMention(
  text: string,
  item: LabeledReference,
): boolean {
  return (
    text.includes(referencePromptToken(item)) ||
    referenceDisplayAliases(item).some((alias) => text.includes(alias))
  );
}

function explicitReferenceAliases(item: LabeledReference): string[] {
  const label = item.label.trim();
  return [
    referencePromptToken(item),
    ...referenceDisplayAliases(item),
    ...(label ? [`[${label}]`] : []),
  ];
}

export function removeReferencesAndReindexPrompt<T extends LabeledReference>(
  text: string,
  refs: readonly T[],
  shouldRemove: (item: T) => boolean,
): { prompt: string; references: T[] } {
  let nextPrompt = text;

  const entries = refs.map((item, index) => {
    let placeholder = `__LUMEN_REFERENCE_${index + 1}__`;
    while (nextPrompt.includes(placeholder)) placeholder += "_";
    const aliases = new Set(explicitReferenceAliases(item));
    for (const alias of Array.from(aliases).sort(
      (left, right) => right.length - left.length,
    )) {
      nextPrompt = nextPrompt.replace(
        new RegExp(escapeRegExp(alias), "g"),
        placeholder,
      );
    }
    return {
      item,
      placeholder,
      removed: shouldRemove(item),
    };
  });

  const kindIndexes: ReferenceLimits = { image: 0, video: 0, audio: 0 };
  const nextReferences = entries
    .filter((entry) => !entry.removed)
    .map((entry) => entry.item)
    .map((item) => {
      kindIndexes[item.kind] += 1;
      const oldIndex = referenceRefIndex(item.ref_id, item.kind);
      const nextIndex = kindIndexes[item.kind];
      return {
        ...item,
        ref_id: referenceRefId(item.kind, nextIndex),
        label:
          oldIndex && item.label === referenceLabel(item.kind, oldIndex)
            ? referenceLabel(item.kind, nextIndex)
            : item.label,
      };
    });

  let retainedIndex = 0;
  for (const entry of entries) {
    const replacement = entry.removed
      ? ""
      : referenceDisplayToken(nextReferences[retainedIndex]);
    if (!entry.removed) retainedIndex += 1;
    nextPrompt = nextPrompt.replace(
      new RegExp(escapeRegExp(entry.placeholder), "g"),
      replacement,
    );
  }

  return {
    prompt: nextPrompt
      .replace(/[ \t]+([，。；：！？,.!?;:])/g, "$1")
      .replace(/[ \t]{2,}/g, " ")
      .trim(),
    references: nextReferences,
  };
}

export function removeReferenceAndReindexPrompt<T extends LabeledReference>(
  text: string,
  refs: readonly T[],
  removed: LabeledReference,
): { prompt: string; references: T[] } {
  const removedToken = referencePromptToken(removed);
  return removeReferencesAndReindexPrompt(
    text,
    refs,
    (item) => referencePromptToken(item) === removedToken,
  );
}

function preservePromptReferenceTokens(
  promptText: string,
  sourceText: string,
  refs: readonly LabeledReference[],
): string {
  if (!promptText.trim() || refs.length === 0) return promptText;
  const missingTokens = refs
    .map((item) => referencePromptToken(item))
    .filter(
      (token) => sourceText.includes(token) && !promptText.includes(token),
    );
  if (missingTokens.length === 0) return promptText;
  const trimmed = promptText.trimEnd();
  const suffix = `保持参考锚点 ${missingTokens.join("、")} 对应的素材约束。`;
  return `${trimmed}${/[。.!?？]$/.test(trimmed) ? " " : "。"}${suffix}`;
}

export function anchorPromptEnhanceCandidates<T extends { prompt: string }>(
  candidates: T[],
  sourceText: string,
  refs: readonly LabeledReference[],
): T[] {
  if (refs.length === 0) return candidates;
  return candidates.map((candidate) => ({
    ...candidate,
    prompt: preservePromptReferenceTokens(candidate.prompt, sourceText, refs),
  }));
}

function referenceMediaPayload(
  item: ReferencePayloadSource,
): VideoReferenceMediaIn {
  if (item.url) {
    return {
      kind: item.kind,
      url: item.url,
      label: item.label,
      ref_id: item.ref_id,
    };
  }
  return {
    kind: item.kind,
    image_id: item.kind === "image" ? (item.image_id ?? null) : null,
    video_id: item.kind === "video" ? (item.video_id ?? null) : null,
    label: item.label,
    ref_id: item.ref_id,
  };
}

export function referencesForVideoAction<T extends LabeledReference>(
  action: VideoAction,
  references: T[],
): T[] {
  return action === "reference" ? references : [];
}

export function promptForVideoAction(
  action: VideoAction,
  prompt: string,
  references: readonly LabeledReference[],
): string {
  const trimmed = prompt.trim();
  return action === "reference"
    ? serializePromptReferenceMentions(trimmed, references)
    : trimmed;
}

export function referencePayloadForVideoAction<
  T extends ReferencePayloadSource,
>(action: VideoAction, references: T[]): VideoReferenceMediaIn[] {
  return referencesForVideoAction(action, references).map(
    referenceMediaPayload,
  );
}
