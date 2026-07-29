import type {
  getMemorySettings,
  listMemoryTimeline,
  patchMemory,
} from "@/lib/apiClient";

export type MemorySettingsData = Awaited<
  ReturnType<typeof getMemorySettings>
>;

export type MemoryTimelineEvent = Awaited<
  ReturnType<typeof listMemoryTimeline>
>["items"][number];

export type MemoryPatchBody = Parameters<typeof patchMemory>[1];

export type MemoryPageQueryScope = {
  userId: string | null | undefined;
  enabled: boolean;
};
