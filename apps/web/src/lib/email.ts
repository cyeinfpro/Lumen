const EMAIL_SPACE_OR_CONTROL_RE = /[\s\u0000-\u001F\u007F]/;
const EMAIL_DOMAIN_LABEL_RE = /^[A-Za-z0-9-]+$/;

export function normalizeEmailInput(value: string): string {
  return value.trim();
}

export function isValidEmailInput(value: string): boolean {
  const email = normalizeEmailInput(value);
  if (email.length < 3 || email.length > 254) return false;
  if (EMAIL_SPACE_OR_CONTROL_RE.test(email)) return false;

  const firstAt = email.indexOf("@");
  if (firstAt <= 0 || firstAt !== email.lastIndexOf("@")) return false;
  if (firstAt === email.length - 1) return false;

  const local = email.slice(0, firstAt);
  const domain = email.slice(firstAt + 1);
  if (local.length > 64 || domain.length > 253) return false;
  if (local.startsWith(".") || local.endsWith(".") || local.includes("..")) {
    return false;
  }

  const labels = domain.split(".");
  if (labels.length < 2) return false;
  return labels.every(
    (label) =>
      label.length > 0 &&
      label.length <= 63 &&
      !label.startsWith("-") &&
      !label.endsWith("-") &&
      EMAIL_DOMAIN_LABEL_RE.test(label),
  );
}
