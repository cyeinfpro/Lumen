import type { MemoryType } from "@/lib/apiClient";

export const TYPE_OPTIONS: Array<{ value: MemoryType; label: string }> = [
  { value: "profile", label: "身份" },
  { value: "preference", label: "偏好" },
  { value: "avoid", label: "禁忌" },
  { value: "project", label: "项目" },
];

export function typeLabel(type: MemoryType | string): string {
  return TYPE_OPTIONS.find((option) => option.value === type)?.label ?? type;
}

export function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function isEmptyFirstRun({
  settingsPending,
  memoriesPending,
  memoryCount,
  onboardingSeen,
}: {
  settingsPending: boolean;
  memoriesPending: boolean;
  memoryCount: number;
  onboardingSeen: number;
}): boolean {
  return (
    !settingsPending &&
    !memoriesPending &&
    memoryCount === 0 &&
    (onboardingSeen & 1) === 0
  );
}

export function removeEditValue(
  values: Record<string, string>,
  id: string,
): Record<string, string> {
  const next = { ...values };
  delete next[id];
  return next;
}
