export interface GenerationSummary {
  id: string;
  created_at: string;
  prompt: string;
  aspect_ratio: string;
  has_ref: boolean;
  quality?: string | null;
  output_format?: string | null;
  size_actual: string;
  parent_generation_id?: string | null;
  action_source?: string | null;
  revised_prompt?: string | null;
  requested_params?: Record<string, unknown> | null;
  effective_params?: Record<string, unknown> | null;
  diagnostics?: Record<string, unknown> | null;
  provider_attempts?: Array<Record<string, unknown>>;
  image: {
    id: string;
    url: string;
    mime?: string;
    display_url?: string;
    preview_url?: string | null;
    thumb_url?: string | null;
    variant_version?: string | null;
    variants?: Partial<
      Record<
        "thumb256" | "preview1024" | "display2048",
        "ready" | "pending" | "missing" | "failed"
      >
    > | null;
    thumb_ready?: boolean | null;
    preview_ready?: boolean | null;
    display_ready?: boolean | null;
    width: number;
    height: number;
    parent_image_id?: string | null;
    metadata_jsonb?: Record<string, unknown> | null;
  };
  message_id: string;
  conversation_id: string;
}

export interface StreamFeedFilters {
  ratio?: string;
  has_ref?: boolean;
  q?: string | null;
}

export interface StreamFeedPage {
  items: GenerationSummary[];
  next_cursor?: string | null;
  total: number;
}
