import {
  POSTER_STYLE_CATEGORY_LABEL,
  POSTER_STYLE_SOURCE_LABEL,
  type PosterStyleCategoryFilter,
  type PosterStyleSourceFilter,
} from "@/lib/apiClient";

export const CATEGORY_TABS: Array<[PosterStyleCategoryFilter, string]> = [
  ["all", POSTER_STYLE_CATEGORY_LABEL.all],
  ["user_favorites", POSTER_STYLE_CATEGORY_LABEL.user_favorites],
  ["illustration", POSTER_STYLE_CATEGORY_LABEL.illustration],
  ["3d", POSTER_STYLE_CATEGORY_LABEL["3d"]],
  ["minimal", POSTER_STYLE_CATEGORY_LABEL.minimal],
  ["retro", POSTER_STYLE_CATEGORY_LABEL.retro],
  ["traditional", POSTER_STYLE_CATEGORY_LABEL.traditional],
  ["photo", POSTER_STYLE_CATEGORY_LABEL.photo],
  ["other", POSTER_STYLE_CATEGORY_LABEL.other],
];

export const SOURCE_FILTERS: Array<[PosterStyleSourceFilter, string]> = [
  ["all", POSTER_STYLE_SOURCE_LABEL.all],
  ["preset", POSTER_STYLE_SOURCE_LABEL.preset],
  ["favorite", POSTER_STYLE_SOURCE_LABEL.favorite],
  ["user_upload", POSTER_STYLE_SOURCE_LABEL.user_upload],
  ["generated", POSTER_STYLE_SOURCE_LABEL.generated],
];

export const SOURCE_LABEL_SHORT: Record<
  Exclude<PosterStyleSourceFilter, "all">,
  string
> = {
  preset: "预设",
  favorite: "收藏",
  user_upload: "上传",
  generated: "生成",
};
