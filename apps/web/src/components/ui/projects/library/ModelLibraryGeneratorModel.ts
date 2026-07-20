import type {
  ApparelModelLibraryGenerateCount,
  ApparelModelLibraryGenerateIn,
  ApparelModelLibraryGenerateMode,
  ModelLibraryAppearance,
  ModelLibraryItemAgeSegment,
} from "@/lib/apiClient";

interface ModelLibraryGenerationValues {
  ageSegment: ModelLibraryItemAgeSegment | "";
  appearance: ModelLibraryAppearance | "";
  autoTag: boolean;
  count: ApparelModelLibraryGenerateCount;
  defaultAgeSegment: ModelLibraryItemAgeSegment;
  extra: string;
  genders: Array<"female" | "male">;
  mode: ApparelModelLibraryGenerateMode;
  referenceImageId: string | null;
  styleTags: string[];
}

export function modelLibrarySubmissionWarning({
  mode,
  referenceImageId,
  referenceUploading,
}: {
  mode: ApparelModelLibraryGenerateMode;
  referenceImageId: string | null;
  referenceUploading: boolean;
}): string | null {
  if (mode !== "reference_image") return null;
  if (referenceUploading) return "参考图仍在上传";
  return referenceImageId ? null : "请先上传参考图";
}

export function buildModelLibraryGenerationBody(
  values: ModelLibraryGenerationValues,
): ApparelModelLibraryGenerateIn {
  const firstGender = values.genders[0];
  return {
    mode: values.mode,
    reference_image_id:
      values.mode === "reference_image" ? values.referenceImageId : null,
    age_segment: resolveAgeSegment(values),
    genders: values.genders.length ? values.genders : undefined,
    gender:
      values.mode === "text"
        ? firstGender ?? "female"
        : firstGender ?? null,
    appearance_direction: values.appearance || null,
    extra_requirements: values.extra.trim() || null,
    style_tags: values.styleTags,
    count: values.count,
    auto_tag: values.autoTag,
  };
}

function resolveAgeSegment(
  values: ModelLibraryGenerationValues,
): ModelLibraryItemAgeSegment | null {
  if (values.mode === "text") {
    return values.ageSegment || values.defaultAgeSegment;
  }
  return values.ageSegment || null;
}
