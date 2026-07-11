// 服饰模特展示工作流（apparel_model_showcase）共享常量与本地化映射。
// 仅前端展示用，后端枚举来源于 apps/api workflows.py。

import {
  Check,
  Download,
  FileText,
  Grid3x3,
  Palette,
  PenLine,
  Scissors,
  Shirt,
  Sparkles,
  Upload,
  WandSparkles,
} from "lucide-react";

export type StepKey =
  | "upload_product"
  | "product_analysis"
  | "model_settings"
  | "model_candidates"
  | "model_approval"
  | "showcase_generation"
  | "quality_review"
  | "delivery";

export interface StepDef {
  key: StepKey;
  label: string;
  short: string;
  Icon: typeof Upload;
}

export const STEPS: StepDef[] = [
  { key: "upload_product", label: "上传商品", short: "上传", Icon: Upload },
  { key: "product_analysis", label: "商品约束", short: "约束", Icon: FileText },
  { key: "model_settings", label: "模特设定", short: "设定", Icon: WandSparkles },
  { key: "model_candidates", label: "模特候选", short: "候选", Icon: Sparkles },
  { key: "model_approval", label: "方案确认", short: "确认", Icon: Check },
  { key: "showcase_generation", label: "商品融合", short: "融合", Icon: Shirt },
  { key: "quality_review", label: "质检返修", short: "质检", Icon: Scissors },
  { key: "delivery", label: "交付", short: "交付", Icon: Download },
];

export const STEP_INDEX: Record<string, number> = STEPS.reduce<Record<string, number>>(
  (acc, step, index) => {
    acc[step.key] = index;
    return acc;
  },
  {},
);

export const STATUS_LABEL: Record<string, string> = {
  draft: "草稿",
  running: "运行中",
  needs_review: "待确认",
  completed: "已完成",
  failed: "失败",
  waiting_input: "待输入",
  approved: "已确认",
  ready: "可选择",
  generating: "生成中",
  selected: "已选择",
  rejected: "未选择",
};

export const RECOMMENDATION_LABEL: Record<string, string> = {
  approve: "通过",
  revise: "需返修",
  pending: "待质检",
};

export const TEMPLATE_VALUE_LABEL: Record<string, string> = {
  premium_studio: "高级棚拍",
  white_ecommerce: "白底主图",
  urban_commute: "质感街拍",
  lifestyle: "精品空间",
  daily_snapshot: "日常随拍",
  natural_phone_snapshot: "自然手机摄影",
  social_seed: "自然种草",
};

export const SHOT_VALUE_LABEL: Record<string, string> = {
  front_full_body: "正面全身",
  natural_pose: "自然姿态",
  detail_half_body: "姿态变化",
  side_or_back: "侧面一张",
};

export const QUALITY_VALUE_LABEL: Record<string, string> = {
  standard: "标准",
  high: "高质量",
  "4k": "4K 终稿",
  subtle: "轻量",
  medium: "中等",
  strong: "明显",
};

export const JSON_KEY_LABEL: Record<string, string> = {
  enabled: "是否开启",
  items: "配饰",
  strength: "强度",
  template: "输出模板",
  shot_plan: "镜头计划",
  aspect_ratio: "画幅比例",
  final_quality: "质量模式",
  output_count: "输出数量",
  scene_strategy: "场景风格",
  scene_variety: "丰富度",
  scene_planner: "AI 导演",
  continuity_anchor: "连续元素",
  allow_pet: "宠物元素",
  allow_background_people: "背景路人",
  reference_image_ids: "参考图数量",
  overall: "总体结论",
  average_score: "平均分",
  revise_count: "需返修数量",
  report_count: "质检数量",
  selected_accessory_image_id: "已选配饰四宫格",
};

export const TEMPLATE_LABELS = [
  ["premium_studio", "高级棚拍"],
  ["urban_commute", "质感街拍"],
  ["lifestyle", "精品空间"],
  ["natural_phone_snapshot", "自然手机摄影"],
  ["daily_snapshot", "日常随拍"],
  ["social_seed", "自然种草"],
  ["white_ecommerce", "白底主图"],
] as const;

export const ASPECT_RATIO_LABELS = [
  ["4:5", "4:5 竖版主图"],
  ["3:4", "3:4 竖版"],
  ["1:1", "1:1 方图"],
  ["9:16", "9:16 竖屏"],
  ["4:3", "4:3 横版"],
  ["3:2", "3:2 横版"],
  ["16:9", "16:9 横图"],
  ["21:9", "21:9 超宽横图"],
] as const;

export const OUTPUT_COUNT_LABELS = [
  [1, "1 张"],
  [2, "2 张"],
  [4, "4 张"],
  [8, "8 张"],
  [16, "16 张"],
] as const;

export type CreateTemplate =
  | "white_ecommerce"
  | "premium_studio"
  | "urban_commute"
  | "lifestyle"
  | "daily_snapshot"
  | "natural_phone_snapshot"
  | "social_seed";

export type CreateAspectRatio = (typeof ASPECT_RATIO_LABELS)[number][0];
export type CreateOutputCount = (typeof OUTPUT_COUNT_LABELS)[number][0];

export const SCENE_ENVIRONMENT_LABELS = [
  ["indoor", "室内"],
  ["outdoor", "室外"],
] as const;

export type CreateSceneEnvironment = (typeof SCENE_ENVIRONMENT_LABELS)[number][0];

export const SCENE_STRATEGY_LABELS = [
  ["natural_series", "自然系列"],
  ["balanced", "商品优先"],
  ["editorial_campaign", "品牌大片"],
] as const;

export type CreateSceneStrategy = (typeof SCENE_STRATEGY_LABELS)[number][0];

export const SCENE_VARIETY_LABELS = [
  ["rich", "丰富"],
  ["safe", "稳妥"],
  ["wild", "大胆"],
] as const;

export type CreateSceneVariety = (typeof SCENE_VARIETY_LABELS)[number][0];

export const CONTINUITY_ANCHOR_LABELS = [
  ["accessory", "保留配饰"],
  ["none", "无连续元素"],
  ["pet", "加宠物"],
  ["location_series", "同地点系列"],
] as const;

export type CreateContinuityAnchor = (typeof CONTINUITY_ANCHOR_LABELS)[number][0];

// 仅这 3 个生活化模板支持 scene_environment 选项；其他模板忽略此字段。
export const SCENE_ENVIRONMENT_TEMPLATES: ReadonlySet<CreateTemplate> = new Set([
  "daily_snapshot",
  "natural_phone_snapshot",
  "social_seed",
]);

export function coerceOutputCount(value: unknown): CreateOutputCount {
  const numberValue = typeof value === "number" ? value : Number(value);
  return OUTPUT_COUNT_LABELS.some(([option]) => option === numberValue)
    ? (numberValue as CreateOutputCount)
    : 4;
}

export const MAX_PRODUCT_IMAGES = 3;
export const MAX_PRODUCT_IMAGE_BYTES = 12 * 1024 * 1024;

export const SHOT_PLAN_DEFAULT = [
  "front_full_body",
  "natural_pose",
  "detail_half_body",
] as const;

// ============================================================================
// 海报工作流（poster_design）共享常量
// 后端 step 来源：apps/api/app/routes/workflows.py 中 POSTER_WORKFLOW_STEPS
// 与 apparel 互不重叠；详情页按 workflow.type 选择对应 STEPS 表。
// ============================================================================

export type PosterStepKey =
  | "copy_input"
  | "style_selection"
  | "copy_analysis"
  | "master_generation"
  | "master_approval"
  | "multi_size_generation"
  | "delivery";

export interface PosterStepDef {
  key: PosterStepKey;
  label: string;
  short: string;
  Icon: typeof Upload;
}

export const POSTER_STEPS: PosterStepDef[] = [
  { key: "copy_input", label: "录入文案", short: "文案", Icon: PenLine },
  { key: "style_selection", label: "选择风格", short: "风格", Icon: Palette },
  { key: "copy_analysis", label: "切分确认", short: "切分", Icon: FileText },
  { key: "master_generation", label: "母版生成", short: "母版", Icon: Sparkles },
  { key: "master_approval", label: "母版选定", short: "选定", Icon: Check },
  { key: "multi_size_generation", label: "多尺寸成品", short: "多尺寸", Icon: Grid3x3 },
  { key: "delivery", label: "交付", short: "交付", Icon: Download },
];

export const POSTER_STEP_INDEX: Record<string, number> = POSTER_STEPS.reduce<
  Record<string, number>
>((acc, step, index) => {
  acc[step.key] = index;
  return acc;
}, {});

export const POSTER_ASPECT_LABELS: ReadonlyArray<readonly [string, string]> = [
  ["1:1", "1:1 方图"],
  ["9:16", "9:16 竖屏"],
  ["16:9", "16:9 横图"],
  ["3:4", "3:4 竖版"],
  ["2:3", "2:3 杂志"],
  ["3:2", "3:2 横版"],
  ["4:3", "4:3 横图"],
  ["4:5", "4:5 主图"],
] as const;

export const POSTER_DEFAULT_TARGET_ASPECTS = [
  "1:1",
  "9:16",
  "16:9",
  "3:4",
] as const;
