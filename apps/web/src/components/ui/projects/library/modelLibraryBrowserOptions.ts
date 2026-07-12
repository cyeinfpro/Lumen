import {
  MODEL_LIBRARY_APPEARANCE_LABEL,
  MODEL_LIBRARY_APPEARANCE_SELECT_OPTIONS,
  type ModelLibraryAgeSegment,
  type ModelLibraryAppearance,
  type ModelLibraryItemAgeSegment,
  type ModelLibrarySource,
} from "@/lib/apiClient";

export type BrowserSource = "all" | ModelLibrarySource | "unsaved_jobs";

export const AGE_TABS: Array<[ModelLibraryAgeSegment, string]> = [
  ["all", "全部"],
  ["user_favorites", "收藏"],
  ["toddler", "幼儿"],
  ["child", "儿童"],
  ["teen", "青少年"],
  ["young_adult", "青年"],
  ["adult", "熟龄"],
  ["middle_aged", "中年"],
  ["senior", "老年"],
];

export const AGE_FOLDER_BY_SEGMENT: Record<ModelLibraryItemAgeSegment, string> =
  {
    user_favorites: "00_user_favorites",
    toddler: "01_toddler",
    child: "02_child",
    teen: "03_teen",
    young_adult: "04_young_adult",
    adult: "05_adult",
    middle_aged: "06_middle_aged",
    senior: "07_senior",
  };

export type ModelLibraryGender = "female" | "male";

export const GENDER_OPTIONS: Array<[ModelLibraryGender, string]> = [
  ["female", "女"],
  ["male", "男"],
];

export function isModelLibraryGender(
  value: unknown,
): value is ModelLibraryGender {
  return value === "female" || value === "male";
}

export function genderLabel(
  value: ModelLibraryGender | null | undefined,
): string {
  if (value === "male") return "男";
  if (value === "female") return "女";
  return "未知";
}

export const SOURCE_FILTERS: Array<[BrowserSource, string]> = [
  ["all", "全部"],
  ["preset", "预设"],
  ["favorite", "收藏"],
  ["user_upload", "上传"],
  ["generated", "生成"],
  ["unsaved_jobs", "待入库"],
];

export const APPEARANCE_TABS: Array<[ModelLibraryAppearance, string]> = [
  ["all", "全部"],
  ...MODEL_LIBRARY_APPEARANCE_SELECT_OPTIONS.map(
    (value) =>
      [value, MODEL_LIBRARY_APPEARANCE_LABEL[value]] as [
        Exclude<ModelLibraryAppearance, "all" | "asian" | "other">,
        string,
      ],
  ),
];

export const AGE_LABEL = Object.fromEntries(AGE_TABS) as Record<
  ModelLibraryAgeSegment,
  string
>;

export const SOURCE_LABEL_SHORT: Record<ModelLibrarySource, string> = {
  preset: "预设",
  favorite: "收藏",
  user_upload: "上传",
  generated: "生成",
};
