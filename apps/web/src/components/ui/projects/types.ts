// 服饰模特展示工作流（apparel_model_showcase）共享常量与本地化映射。
// 仅前端展示用，后端枚举来源于 apps/api workflows.py。

import {
  Check,
  Download,
  FileText,
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
  { key: "product_analysis", label: "商品理解", short: "理解", Icon: FileText },
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
  premium_studio: "高级灰棚拍",
  white_ecommerce: "白底电商图",
  urban_commute: "城市通勤场景",
  lifestyle: "智能生活场景",
  social_seed: "社媒种草图",
};

export const SHOT_VALUE_LABEL: Record<string, string> = {
  front_full_body: "正面全身",
  natural_pose: "自然姿态",
  detail_half_body: "半身细节",
  side_or_back: "侧面或背面",
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
  items: "饰品",
  strength: "强度",
  template: "输出模板",
  shot_plan: "镜头计划",
  aspect_ratio: "画幅比例",
  final_quality: "质量模式",
  output_count: "输出数量",
  reference_image_ids: "参考图数量",
  overall: "总体结论",
  average_score: "平均分",
  revise_count: "需返修数量",
  report_count: "质检数量",
  selected_accessory_image_id: "已选饰品图",
};

export const TEMPLATE_LABELS = [
  ["premium_studio", "高级灰棚拍"],
  ["white_ecommerce", "白底电商图"],
  ["urban_commute", "城市通勤场景"],
  ["lifestyle", "智能生活场景"],
  ["social_seed", "社媒种草图"],
] as const;

export type CreateTemplate =
  | "white_ecommerce"
  | "premium_studio"
  | "urban_commute"
  | "lifestyle"
  | "social_seed";

export const MAX_PRODUCT_IMAGES = 3;
export const MAX_PRODUCT_IMAGE_BYTES = 12 * 1024 * 1024;

export const SHOT_PLAN_DEFAULT = [
  "front_full_body",
  "natural_pose",
  "detail_half_body",
  "side_or_back",
] as const;
