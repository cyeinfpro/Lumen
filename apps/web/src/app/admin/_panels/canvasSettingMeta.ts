import { SlidersHorizontal } from "lucide-react";

export const CANVAS_ENABLED_KEY = "canvas.enabled";

export const CANVAS_SETTING_META = {
  group: "ui",
  title: "开放无限画布",
  summary: "独立控制无限画布入口和 Canvas API。",
  detail: "关闭时保留已有数据，但不允许访问或执行。",
  kind: "toggle",
  icon: SlidersHorizontal,
  defaultValue: "0",
  recommended: "完成迁移、计费回归和浏览器验收后再灰度开启。",
  keywords: ["canvas", "infinite", "画布", "工作流", "灰度", "入口"],
} as const;
