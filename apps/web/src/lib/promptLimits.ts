export const MAX_PROMPT_CHARS = 10_000;

export const PROMPT_TOO_LONG_MESSAGE = `提示词不能超过 ${MAX_PROMPT_CHARS} 字，请精简后再发送`;

export function isPromptTooLong(text: string): boolean {
  return text.length > MAX_PROMPT_CHARS;
}

export function clampPromptForRequest(text: string): string {
  return text.length > MAX_PROMPT_CHARS
    ? text.slice(0, MAX_PROMPT_CHARS).trimEnd()
    : text;
}

export function appendPromptWithinLimit(base: string, suffix: string): string {
  const cleanBase = base.trim();
  const cleanSuffix = suffix.trim();
  if (!cleanBase) return clampPromptForRequest(cleanSuffix);
  if (!cleanSuffix) return clampPromptForRequest(cleanBase);

  const separator = "\n\n";
  const full = `${cleanBase}${separator}${cleanSuffix}`;
  if (full.length <= MAX_PROMPT_CHARS) return full;

  const suffixBudget = Math.max(0, MAX_PROMPT_CHARS - separator.length);
  const safeSuffix =
    cleanSuffix.length > suffixBudget
      ? cleanSuffix.slice(0, suffixBudget).trimEnd()
      : cleanSuffix;
  const baseBudget = MAX_PROMPT_CHARS - separator.length - safeSuffix.length;
  if (baseBudget <= 0) return clampPromptForRequest(safeSuffix);

  const safeBase = cleanBase.slice(0, baseBudget).trimEnd();
  return safeBase ? `${safeBase}${separator}${safeSuffix}` : safeSuffix;
}
