import type { ConversationSummary } from "@/lib/apiClient";

export type Bucket = "today" | "yesterday" | "last7" | "older";
export type TabKind = "active" | "archived";

export const BUCKET_ORDER: Bucket[] = [
  "today",
  "yesterday",
  "last7",
  "older",
];

export const BUCKET_LABEL: Record<Bucket, string> = {
  today: "今天",
  yesterday: "昨天",
  last7: "本周",
  older: "更早",
};

export const SKELETON_ROWS = [
  { id: "first", titleWidth: 62 },
  { id: "second", titleWidth: 70 },
  { id: "third", titleWidth: 56 },
  { id: "fourth", titleWidth: 78 },
  { id: "fifth", titleWidth: 50 },
] as const;

export type ConversationDrawerGroups = Record<
  Bucket,
  ConversationSummary[]
>;

export interface ConversationDrawerModel {
  allConvs: ConversationSummary[];
  activeTotal: number;
  archivedTotal: number;
  filtered: ConversationSummary[];
  grouped: ConversationDrawerGroups;
  hasResults: boolean;
}

export function titleOf(conversation: ConversationSummary): string {
  return conversation.title?.trim() || "New Canvas";
}

function dayKeyOf(iso: string): Bucket {
  const timestamp = Date.parse(iso);
  if (!Number.isFinite(timestamp)) return "older";

  const now = new Date();
  const date = new Date(timestamp);
  const startOfDay = (value: Date) =>
    new Date(
      value.getFullYear(),
      value.getMonth(),
      value.getDate(),
    ).getTime();
  const todayStart = startOfDay(now);
  const yesterdayStart = todayStart - 24 * 3600 * 1000;
  const last7Start = todayStart - 7 * 24 * 3600 * 1000;

  if (date.getTime() >= todayStart) return "today";
  if (date.getTime() >= yesterdayStart) return "yesterday";
  if (date.getTime() >= last7Start) return "last7";
  return "older";
}

function emptyGroups(): ConversationDrawerGroups {
  return {
    today: [],
    yesterday: [],
    last7: [],
    older: [],
  };
}

function groupConversations(
  conversations: ConversationSummary[],
): ConversationDrawerGroups {
  const groups = emptyGroups();
  for (const conversation of conversations) {
    groups[dayKeyOf(conversation.last_activity_at)].push(conversation);
  }
  return groups;
}

export function deriveConversationDrawerModel(
  pages:
    | ReadonlyArray<{ items: ConversationSummary[] }>
    | undefined,
  query: string,
  tab: TabKind,
): ConversationDrawerModel {
  const allConvs = (pages ?? []).flatMap((page) => page.items);
  const activeTotal = allConvs.filter(
    (conversation) => !conversation.archived,
  ).length;
  const archivedTotal = allConvs.filter(
    (conversation) => conversation.archived,
  ).length;
  const normalizedQuery = query.trim().toLowerCase();
  const filtered = allConvs.filter((conversation) => {
    const matchesTab =
      tab === "archived"
        ? conversation.archived
        : !conversation.archived;
    return (
      matchesTab &&
      (!normalizedQuery ||
        titleOf(conversation).toLowerCase().includes(normalizedQuery))
    );
  });

  return {
    allConvs,
    activeTotal,
    archivedTotal,
    filtered,
    grouped: groupConversations(filtered),
    hasResults: filtered.length > 0,
  };
}

export function isInitialConversationLoad(
  isLoading: boolean,
  count: number,
): boolean {
  return isLoading && count === 0;
}
