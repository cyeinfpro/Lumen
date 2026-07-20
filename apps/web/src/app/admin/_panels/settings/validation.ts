import { copy } from "@/lib/copy";
import {
  formatPlainNumber,
  getSettingMeta,
  normalizePublicBaseUrlInput,
  type Op,
  type SettingMeta,
} from "./model";

type OperationValidation = {
  error?: string;
  value?: string;
};

function validateNumericValue(
  raw: string,
  meta: SettingMeta,
): OperationValidation {
  if (raw === "") return { error: "需填数值" };
  const value = Number(raw);
  if (!Number.isFinite(value)) return { error: "数字格式错误" };
  if (meta.kind === "integer" && !Number.isInteger(value)) {
    return { error: "不支持小数" };
  }
  if (meta.min != null && value < meta.min) {
    return {
      error: `不能小于 ${formatPlainNumber(meta.min)}${meta.unit ?? ""}`,
    };
  }
  if (meta.max != null && value > meta.max) {
    return {
      error: `不能大于 ${formatPlainNumber(meta.max)}${meta.unit ?? ""}`,
    };
  }
  return {
    value:
      meta.kind === "integer" ? String(Math.trunc(value)) : String(value),
  };
}

function validateToggleValue(raw: string): OperationValidation {
  return raw === "0" || raw === "1"
    ? { value: raw }
    : { error: "需选开启/关闭" };
}

function validateEnumValue(
  raw: string,
  meta: SettingMeta,
): OperationValidation {
  return meta.choices?.some((option) => option.value === raw)
    ? { value: raw }
    : { error: "无效选项" };
}

function validateUrlValue(raw: string): OperationValidation {
  const normalized = normalizePublicBaseUrlInput(raw);
  return normalized
    ? { value: normalized }
    : { error: "需 http(s) 根域名，无路径" };
}

function validateTextValue(raw: string): OperationValidation {
  return raw === "" ? { error: copy.error.required } : { value: raw };
}

function validateSettingOperation(
  key: string,
  op: Op,
): OperationValidation {
  if (op.kind === "clear") return { value: "" };
  const raw = op.value.trim();
  const meta = getSettingMeta(key);
  if (meta.kind === "integer" || meta.kind === "decimal") {
    return validateNumericValue(raw, meta);
  }
  if (meta.kind === "toggle") return validateToggleValue(raw);
  if (meta.kind === "enum") return validateEnumValue(raw, meta);
  if (meta.kind === "url") return validateUrlValue(raw);
  return validateTextValue(raw);
}

export function validateSettingOps(ops: Record<string, Op>) {
  const errors: Record<string, string> = {};
  const payload: { key: string; value: string }[] = [];
  for (const [key, op] of Object.entries(ops)) {
    const result = validateSettingOperation(key, op);
    if (result.error) {
      errors[key] = result.error;
      continue;
    }
    payload.push({ key, value: result.value ?? "" });
  }
  return {
    ok: Object.keys(errors).length === 0,
    errors,
    payload,
  };
}
