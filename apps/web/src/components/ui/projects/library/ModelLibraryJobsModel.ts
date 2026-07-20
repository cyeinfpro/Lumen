import {
  imageBinaryUrl,
  MODEL_LIBRARY_APPEARANCE_LABEL,
  type ApparelModelLibraryJob,
  type ModelLibraryItemAgeSegment,
} from "@/lib/apiClient";

type AppearanceKey = keyof typeof MODEL_LIBRARY_APPEARANCE_LABEL;

export const AGE_LABEL: Record<ModelLibraryItemAgeSegment, string> = {
  user_favorites: "用户收藏",
  toddler: "幼儿",
  child: "儿童",
  teen: "青少年",
  young_adult: "青年",
  adult: "熟龄",
  middle_aged: "中年",
  senior: "老年",
};

export interface ReferenceSummaryModel {
  imageUrl: string;
  notes: string | null;
  tokens: string[];
}

export function buildReferenceSummary(
  job: ApparelModelLibraryJob,
): ReferenceSummaryModel | null {
  if (!job.reference_image_id) return null;
  return {
    imageUrl:
      job.reference_image_url || imageBinaryUrl(job.reference_image_id),
    notes: job.extracted_profile?.notes ?? null,
    tokens: referenceTokens(job),
  };
}

function referenceTokens(job: ApparelModelLibraryJob): string[] {
  const profile = job.extracted_profile;
  const tokens: string[] = [];
  const age = profile?.age_segment || job.age_segment;
  if (age) tokens.push(AGE_LABEL[age] ?? age);
  const gender = profile?.gender || job.gender;
  if (gender) tokens.push(genderLabel(gender));
  const appearance =
    profile?.appearance_direction || job.appearance_direction;
  if (appearance) tokens.push(appearanceLabel(appearance));
  appendStyleTags(tokens, profile?.style_tags ?? []);
  return tokens;
}

function genderLabel(gender: string): string {
  if (gender === "male") return "男";
  if (gender === "female") return "女";
  return gender;
}

function appearanceLabel(appearance: string): string {
  const key = appearance as AppearanceKey;
  return MODEL_LIBRARY_APPEARANCE_LABEL[key] ?? appearance;
}

function appendStyleTags(tokens: string[], tags: string[]) {
  for (const tag of tags) {
    if (tag && tokens.length < 8) tokens.push(tag);
  }
}
