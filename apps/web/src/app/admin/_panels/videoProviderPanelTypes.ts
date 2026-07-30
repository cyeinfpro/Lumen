import type { VideoProviderKind } from "@/lib/types";

export type VideoAction = "t2v" | "i2v" | "reference";

export type ModelDraft = {
  _key: number;
  model: string;
  t2v: string;
  i2v: string;
  reference: string;
};

export type Draft = {
  _key: number;
  original_name?: string;
  name: string;
  kind: VideoProviderKind;
  base_url: string;
  api_key: string;
  access_key_id: string;
  secret_access_key: string;
  project_name: string;
  region: string;
  enabled: boolean;
  priority: number;
  weight: number;
  concurrency: number;
  supports_idempotency: boolean;
  proxy: string;
  models: ModelDraft[];
};

export type Issue = {
  severity: "error" | "warning";
  message: string;
};

export type ProviderSummary = {
  name: string;
  kind: VideoProviderKind;
  enabled: boolean;
  hasKey: boolean;
  capabilities: Set<VideoAction>;
  modelNames: string[];
  concurrency: number;
  issues: Issue[];
};
