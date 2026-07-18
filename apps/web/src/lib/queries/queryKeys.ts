import type {
  ApparelModelLibraryJobsOpts,
  ListConversationsOpts,
  ModelLibraryAgeSegment,
  ModelLibraryAppearance,
  ModelLibrarySource,
  PosterStyleJobsOpts,
  PosterStyleListOpts,
} from "../apiClient";

const UNKNOWN_USER_QUERY_SCOPE = "__identity_unknown__";

function userQueryScope(userId: string | null | undefined) {
  return [
    "user",
    typeof userId === "string" && userId.trim().length > 0
      ? userId
      : UNKNOWN_USER_QUERY_SCOPE,
  ] as const;
}

function userQueryKeys(userId: string | null | undefined) {
  const scope = userQueryScope(userId);

  return {
    myShares: () => [...scope, "me", "shares"] as const,
    mySessions: () => [...scope, "me", "sessions"] as const,
    conversationsAll: () => [...scope, "conversations"] as const,
    conversations: (opts?: ListConversationsOpts) =>
      [...scope, "conversations", opts ?? {}] as const,
    conversationsInfiniteAll: () =>
      [...scope, "conversations", "infinite"] as const,
    conversationsInfinite: (params: { limit: number; q?: string }) =>
      [...scope, "conversations", "infinite", params] as const,
    conversationDetail: (convId: string) =>
      [...scope, "conversations", "detail", convId] as const,
    conversationContext: (convId: string) =>
      [...scope, "conversations", convId, "context"] as const,
    workflowsAll: () => [...scope, "workflows"] as const,
    workflows: (params?: { type?: string; limit?: number }) =>
      [...scope, "workflows", params ?? {}] as const,
    workflow: (id: string) => [...scope, "workflows", id] as const,
    storyboardsAll: () => [...scope, "storyboards"] as const,
    storyboards: (params?: { cursor?: string | null; limit?: number }) =>
      [...scope, "storyboards", params ?? {}] as const,
    storyboard: (id: string) => [...scope, "storyboards", id] as const,
    apparelModelLibraryLists: () =>
      [...scope, "workflows", "apparel_model_library", "list"] as const,
    apparelModelLibrary: (params?: {
      age_segment?: ModelLibraryAgeSegment;
      source?: "all" | ModelLibrarySource;
      appearance?: ModelLibraryAppearance;
      q?: string;
    }) =>
      [
        ...scope,
        "workflows",
        "apparel_model_library",
        "list",
        params ?? {},
      ] as const,
    apparelModelLibraryJobs: () =>
      [...scope, "workflows", "apparel_model_library", "jobs"] as const,
    apparelModelLibraryJobsList: (params?: ApparelModelLibraryJobsOpts) =>
      [
        ...scope,
        "workflows",
        "apparel_model_library",
        "jobs",
        params ?? {},
      ] as const,
    apparelModelLibraryJobsInfinite: (params: { limit: number }) =>
      [
        ...scope,
        "workflows",
        "apparel_model_library",
        "jobs",
        "infinite",
        params,
      ] as const,
    posterStylesAll: () => [...scope, "poster-styles"] as const,
    posterStyleLists: () => [...scope, "poster-styles", "list"] as const,
    posterStyles: (params?: PosterStyleListOpts) =>
      [...scope, "poster-styles", "list", params ?? {}] as const,
    posterStyleDetails: () => [...scope, "poster-styles", "detail"] as const,
    posterStyle: (id: string) =>
      [...scope, "poster-styles", "detail", id] as const,
    posterStyleJobs: (params?: PosterStyleJobsOpts) =>
      [...scope, "poster-styles", "jobs", params ?? {}] as const,
  };
}

export const qk = {
  allowedEmails: () => ["admin", "allowed_emails"] as const,
  adminUserHistory: (userId: string) =>
    ["admin", "users", userId, "history"] as const,
  adminRequestEvents: (params?: {
    limit?: number;
    kind?: "all" | "generation" | "completion";
    status?: string;
    range?: "24h" | "7d" | "30d";
  }) => ["admin", "request_events", params ?? {}] as const,
  inviteLinks: () => ["admin", "invite_links"] as const,
  publicInvite: (token: string) => ["invite", token] as const,
  systemSettings: () => ["admin", "settings"] as const,
  adminModels: () => ["admin", "models"] as const,
  providers: () => ["admin", "providers"] as const,
  videoProviders: () => ["admin", "providers", "video"] as const,
  providerStats: () => ["admin", "providers", "stats"] as const,
  adminProxies: () => ["admin", "proxies"] as const,
  adminUpdateStatus: () => ["admin", "update", "status"] as const,
  adminUpdateVersion: () => ["admin", "update", "version"] as const,
  adminUpdateCheck: (force: boolean) =>
    ["admin", "update", "check", { force }] as const,
  adminReleases: () => ["admin", "releases"] as const,
  adminStorage: () => ["admin", "storage"] as const,
  systemPrompts: () => ["system_prompts"] as const,
  user: userQueryKeys,
};
