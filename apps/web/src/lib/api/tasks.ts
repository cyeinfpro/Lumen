import type {
  RecommendedErrorAction,
  StructuredAttachment,
} from "../types";

export type GenerationTaskStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "canceled";
export type CompletionTaskStatus =
  | "queued"
  | "streaming"
  | "succeeded"
  | "failed"
  | "canceled";
export type TaskStatus = GenerationTaskStatus | CompletionTaskStatus;

// 对齐后端 GenerationOut / CompletionOut / ImageOut（packages/core/lumen_core/schemas.py）。
export interface BackendGeneration {
  id: string;
  message_id: string;
  conversation_id?: string | null;
  project_id?: string | null;
  user_api_credential_id?: string | null;
  upstream_supplier_id?: string | null;
  parent_generation_id?: string | null;
  action: string;
  prompt: string;
  size_requested: string;
  aspect_ratio: string;
  input_image_ids: string[];
  primary_input_image_id: string | null;
  status: GenerationTaskStatus;
  progress_stage: string;
  stage?: string | null;
  substage?: string | null;
  queue_position?: number | null;
  retrying?: boolean;
  waiting_provider?: boolean;
  cancelled?: boolean;
  retryable?: boolean;
  recommended_actions?: RecommendedErrorAction[];
  thumb_url?: string | null;
  created_at?: string | null;
  attempt: number;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  is_dual_race_bonus?: boolean;
  billing_free?: boolean;
  billing_label?: string | null;
  billing_exempt_reason?: string | null;
  diagnostics?: Record<string, unknown>;
  revised_prompt?: string | null;
  requested_params?: Record<string, unknown> | null;
  effective_params?: Record<string, unknown> | null;
  provider_attempts?: Array<Record<string, unknown>>;
  source?: string | null;
  action_source?: string | null;
  trace_id?: string | null;
  attachment_roles?: StructuredAttachment[];
  source_image_id?: string | null;
  queue_lane?: string | null;
  workflow_type?: string | null;
  workflow_step_key?: string | null;
  pixel_count?: number | null;
  size_bucket?: string | null;
  cost_class?: string | null;
  queue_wait_ms?: number | null;
}

export interface BackendCompletion {
  id: string;
  message_id: string;
  conversation_id?: string | null;
  project_id?: string | null;
  source?: string | null;
  user_api_credential_id?: string | null;
  upstream_supplier_id?: string | null;
  upstream_request?: Record<string, unknown> | null;
  model: string;
  input_image_ids: string[];
  text: string;
  tokens_in: number;
  tokens_out: number;
  status: CompletionTaskStatus;
  progress_stage: string;
  stage?: string | null;
  substage?: string | null;
  retrying?: boolean;
  waiting_provider?: boolean;
  cancelled?: boolean;
  retryable?: boolean;
  recommended_actions?: RecommendedErrorAction[];
  created_at?: string | null;
  attempt: number;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  queue_lane?: string | null;
  workflow_type?: string | null;
  workflow_step_key?: string | null;
  pixel_count?: number | null;
  size_bucket?: string | null;
  cost_class?: string | null;
  queue_wait_ms?: number | null;
}

export interface BackendImageMeta {
  id: string;
  source: string;
  parent_image_id: string | null;
  owner_generation_id?: string | null;
  width: number;
  height: number;
  mime: string;
  blurhash: string | null;
  url: string;
  display_url?: string | null;
  preview_url?: string | null;
  thumb_url?: string | null;
  metadata_jsonb?: Record<string, unknown> | null;
  is_dual_race_bonus?: boolean;
  billing_free?: boolean;
  billing_label?: string | null;
  billing_exempt_reason?: string | null;
}
