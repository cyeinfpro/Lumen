export interface ModelLibraryStyleTaggedItem {
  style_tags: readonly string[];
}

function normalizeStyleTag(value: string): string {
  return value.trim().toLowerCase();
}

export function collectModelLibraryStyleTags(
  items: readonly ModelLibraryStyleTaggedItem[],
  limit = 10,
): string[] {
  const tags = new Map<string, { label: string; count: number; order: number }>();
  let order = 0;
  for (const item of items) {
    const seen = new Set<string>();
    for (const rawTag of item.style_tags) {
      const label = rawTag.trim();
      const normalized = normalizeStyleTag(label);
      if (!normalized || seen.has(normalized)) continue;
      seen.add(normalized);
      const current = tags.get(normalized);
      if (current) {
        current.count += 1;
      } else {
        tags.set(normalized, { label, count: 1, order });
        order += 1;
      }
    }
  }
  return [...tags.values()]
    .sort((left, right) => right.count - left.count || left.order - right.order)
    .slice(0, Math.max(0, limit))
    .map(({ label }) => label);
}

export function filterModelLibraryItemsByStyleTag<
  T extends ModelLibraryStyleTaggedItem,
>(items: readonly T[], styleTag: string): T[] {
  const normalizedFilter = normalizeStyleTag(styleTag);
  if (!normalizedFilter) return [...items];
  return items.filter((item) =>
    item.style_tags.some(
      (candidate) => normalizeStyleTag(candidate) === normalizedFilter,
    ),
  );
}
