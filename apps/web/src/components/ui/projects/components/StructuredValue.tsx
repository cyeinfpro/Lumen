import { cn } from "@/lib/utils";

import {
  JSON_KEY_LABEL,
  QUALITY_VALUE_LABEL,
  RECOMMENDATION_LABEL,
  SHOT_VALUE_LABEL,
  STATUS_LABEL,
  TEMPLATE_VALUE_LABEL,
} from "../types";

const FIELD_LABELS: Record<string, string> = {
  main_title: "主标题",
  subtitle: "副标题",
  selling_points: "卖点",
  cta: "行动召唤",
  price: "价格",
  tone: "语气",
  info_density: "信息密度",
  title: "标题",
  mood: "氛围",
  category: "品类",
  color: "主色",
  material_guess: "材质",
  silhouette: "版型",
  key_details: "关键细节",
  risks: "风险",
  must_preserve: "商品还原点",
  styling_recommendations: "推荐配饰",
  background_recommendation: "推荐背景",
  target_aspects: "目标尺寸",
  brand_assets: "品牌素材",
  copy_analysis: "文案切分",
};

const VALUE_LABELS: ReadonlyArray<Record<string, string>> = [
  STATUS_LABEL,
  RECOMMENDATION_LABEL,
  TEMPLATE_VALUE_LABEL,
  SHOT_VALUE_LABEL,
  QUALITY_VALUE_LABEL,
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function hasContent(value: unknown): boolean {
  if (value == null || value === "") return false;
  if (Array.isArray(value)) return value.length > 0;
  if (isRecord(value)) return Object.values(value).some(hasContent);
  return true;
}

function fieldLabel(key: string): string {
  return JSON_KEY_LABEL[key] ?? FIELD_LABELS[key] ?? key.replaceAll("_", " ");
}

function scalarLabel(value: string | number | boolean): string {
  if (typeof value === "boolean") return value ? "是" : "否";
  const raw = String(value);
  return VALUE_LABELS.find((labels) => labels[raw])?.[raw] ?? raw;
}

function isScalar(value: unknown): value is string | number | boolean {
  return (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  );
}

export function StructuredValue({
  value,
  emptyLabel = "暂无",
  className,
}: {
  value: unknown;
  emptyLabel?: string;
  className?: string;
}) {
  if (!hasContent(value)) {
    return (
      <span className={cn("type-body-sm text-[var(--fg-2)]", className)}>
        {emptyLabel}
      </span>
    );
  }

  if (isScalar(value)) {
    return (
      <span
        className={cn(
          "type-body-sm whitespace-pre-wrap break-words text-[var(--fg-1)]",
          className,
        )}
      >
        {scalarLabel(value)}
      </span>
    );
  }

  if (Array.isArray(value)) {
    const scalarItems = value.filter(isScalar);
    if (scalarItems.length === value.length) {
      return (
        <ul className={cn("flex min-w-0 flex-wrap gap-1.5", className)}>
          {scalarItems.map((item, index) => (
            <li
              key={`${String(item)}-${index}`}
              className="type-caption max-w-full break-words rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-[var(--fg-1)]"
            >
              {scalarLabel(item)}
            </li>
          ))}
        </ul>
      );
    }

    return (
      <ul className={cn("list-group min-w-0", className)}>
        {value.map((item, index) => (
          <li key={index} className="list-row min-w-0 py-2">
            <StructuredValue value={item} />
          </li>
        ))}
      </ul>
    );
  }

  if (!isRecord(value)) {
    return (
      <span
        className={cn(
          "type-body-sm whitespace-pre-wrap break-words text-[var(--fg-1)]",
          className,
        )}
      >
        {String(value)}
      </span>
    );
  }

  const entries = Object.entries(value)
    .filter(([, item]) => hasContent(item))
    .sort(([left], [right]) =>
      fieldLabel(left).localeCompare(fieldLabel(right), "zh-CN"),
    );

  if (!entries.length) {
    return (
      <span className={cn("type-body-sm text-[var(--fg-2)]", className)}>
        {emptyLabel}
      </span>
    );
  }

  return (
    <dl
      className={cn(
        "min-w-0 divide-y divide-[var(--border-subtle)]",
        className,
      )}
    >
      {entries.map(([key, item]) => (
        <div
          key={key}
          className="grid min-w-0 gap-1.5 py-2 first:pt-0 last:pb-0 sm:grid-cols-[minmax(6rem,0.38fr)_minmax(0,1fr)] sm:gap-3"
        >
          <dt className="type-caption min-w-0 break-words text-[var(--fg-2)]">
            {fieldLabel(key)}
          </dt>
          <dd className="min-w-0">
            <StructuredValue value={item} />
          </dd>
        </div>
      ))}
    </dl>
  );
}
