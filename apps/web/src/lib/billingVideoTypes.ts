// ---------- Billing / Wallet ----------

export interface MoneyOut {
  micro: number;
  rmb: string;
}

export interface WalletActivity24hOut {
  topup: MoneyOut;
  spend: MoneyOut;
}

export interface WalletOut {
  mode: "wallet" | "byok";
  balance: MoneyOut | null;
  hold: MoneyOut | null;
  low_balance_threshold?: MoneyOut | null;
  frozen: boolean;
  activity_24h?: WalletActivity24hOut;
}

export interface WalletTransactionOut {
  id: string;
  kind: string;
  amount: MoneyOut;
  balance_after: MoneyOut;
  hold_after: MoneyOut;
  ref_type: string | null;
  ref_id: string | null;
  meta: Record<string, unknown>;
  created_at: string;
  created_by_admin: string | null;
}

export interface WalletTransactionListOut {
  items: WalletTransactionOut[];
  next_cursor?: string | null;
}

export interface BillingWindowOut {
  used_micro: number;
  limit_micro: number;
  resets_at: string | null;
}

export interface BillingUsageByKindOut {
  input: number;
  output: number;
  cache_read: number;
  cache_creation: number;
  image: number;
  reasoning: number;
}

export interface BillingSnapshotOut {
  balance_micro: number;
  billing_rate_multiplier: string;
  windows: Record<string, BillingWindowOut>;
  by_kind_30d: BillingUsageByKindOut;
}

export type {
  VideoAssetCapabilitiesOut,
  VideoAssetCapabilityReason,
  VideoAssetCreateIn,
  VideoAssetDeleteResultOut,
  VideoAssetGroupCreateIn,
  VideoAssetGroupListOut,
  VideoAssetGroupOut,
  VideoAssetGroupPatchIn,
  VideoAssetListOut,
  VideoAssetOperationAction,
  VideoAssetOperationErrorOut,
  VideoAssetOperationOut,
  VideoAssetOperationResult,
  VideoAssetOut,
  VideoAssetPatchIn,
  VideoAssetQuotaLimitsOut,
  VideoAssetQuotaUsageOut,
  VideoAssetStatus,
  VideoAssetType,
} from "./videoAssetTypes";
// ABI fields live in videoAssetTypes.ts:
// quotas: VideoAssetQuotaLimitsOut
// delivery_generation: number

export interface AdminBillingUsageOut {
  user_id: string;
  balance_micro: number;
  billing_rate_multiplier: string;
  range_start: string;
  range_end: string;
  windows: Record<string, BillingWindowOut>;
  by_kind_30d: BillingUsageByKindOut;
  total_micro: number;
  transaction_count: number;
}

export interface PricingRuleOut {
  id: string;
  scope: "image_size" | "chat_model" | "video";
  key: string;
  variant: string;
  unit:
    | "per_image"
    | "per_1k_tokens_in"
    | "per_1k_tokens_out"
    | "per_1k_tokens_cache_read"
    | "per_1k_tokens_cache_creation"
    | "per_1k_tokens_cache_creation_5m"
    | "per_1k_tokens_cache_creation_1h"
    | "per_1k_tokens_image_output"
    | "per_1k_tokens_reasoning"
    | "per_1k_tokens_input_priority"
    | "per_1k_tokens_output_priority"
    | "per_1k_tokens_cache_read_priority"
    | "long_context_threshold"
    | "long_context_input_multiplier"
    | "long_context_output_multiplier"
    | "per_mtoken";
  price: MoneyOut;
  priority: number;
  enabled: boolean;
  note: string | null;
  created_at: string;
  updated_at: string;
}

export interface PricingRulesOut {
  items: PricingRuleOut[];
  image_size_thresholds?: Record<string, number> | null;
  billing_enabled?: boolean | null;
  show_estimate_in_composer?: boolean | null;
}

export type VideoAction = "t2v" | "i2v" | "reference";
export type VideoPricingAction =
  | VideoAction
  | "reference_image"
  | "reference_video"
  | "reference_audio";
export type VideoStatus =
  | "queued"
  | "submitting"
  | "submit_unknown"
  | "submitted"
  | "running"
  | "succeeded"
  | "failed"
  | "canceled"
  | "expired";
export type VideoStage =
  | "queued"
  | "submitting"
  | "rendering"
  | "fetching"
  | "storing"
  | "billing"
  | "finished";

export interface VideoOut {
  id: string;
  url: string;
  poster_url?: string | null;
  width: number;
  height: number;
  duration_ms: number;
  fps?: number | null;
  has_audio: boolean;
  mime: string;
  size_bytes?: number | null;
  faststart?: boolean | null;
  created_at?: string | null;
}

export interface VideoUploadOut extends VideoOut {
  created: boolean;
}

export interface VideoTemporaryDownloadOut {
  source: string;
  url: string;
  expires_at: string;
  expires_in_s: number;
}

export interface VideoReferenceMediaIn {
  kind: "image" | "video" | "audio";
  image_id?: string | null;
  video_id?: string | null;
  url?: string | null;
  label?: string | null;
  ref_id?: string | null;
}

export interface VideoReferenceMediaOut {
  kind: "image" | "video" | "audio";
  image_id?: string | null;
  video_id?: string | null;
  url?: string | null;
  label?: string | null;
  ref_id?: string | null;
  mime?: string | null;
}

export interface VideoCreateIn {
  action: VideoAction;
  model: string;
  prompt: string;
  input_image_id?: string | null;
  reference_media?: VideoReferenceMediaIn[];
  duration_s: number;
  resolution: string;
  aspect_ratio: string;
  generate_audio?: boolean;
  seed?: number | null;
  watermark?: boolean;
  idempotency_key: string;
}

export interface VideoPromptEnhanceIn {
  text?: string;
  action?: VideoAction;
  model?: string;
  duration_s?: number | null;
  resolution?: string | null;
  aspect_ratio?: string | null;
  generate_audio?: boolean | null;
  input_image_id?: string | null;
  reference_media?: VideoReferenceMediaIn[];
  variant_count?: number;
}

export interface VideoPriceOptionOut {
  model: string;
  action: VideoPricingAction;
  resolution?: string | null;
  variant?: string | null;
  unit: "per_mtoken";
  price: MoneyOut;
  enabled: boolean;
  note?: string | null;
}

export interface VideoImageConstraintsOut {
  min_side_px?: number | null;
  max_side_px?: number | null;
  min_aspect_ratio?: number | null;
  max_aspect_ratio?: number | null;
  min_width_px?: number | null;
  max_width_px?: number | null;
  min_height_px?: number | null;
  max_height_px?: number | null;
  max_bytes?: number | null;
  mime_types?: string[];
}

export interface VideoReferenceMediaCapabilitiesOut {
  limits?: Partial<Record<VideoReferenceMediaIn["kind"], number>>;
  total_limit?: number | null;
  allow_audio_only?: boolean;
  image_constraints?: VideoImageConstraintsOut | null;
}

export interface VideoParameterDefaultsOut {
  resolution?: string | null;
  aspect_ratio?: string | null;
  duration_s?: number | null;
  generate_audio?: boolean | null;
}

export interface VideoActionCapabilityOut {
  enabled?: boolean;
  resolutions?: string[];
  aspect_ratios?: string[];
  durations_s?: number[];
  durations_by_resolution?: Partial<Record<string, number[]>>;
  generate_audio?: boolean;
  defaults?: VideoParameterDefaultsOut;
  billing_model?: string | null;
  pricing_action?: VideoPricingAction | null;
  reference_media?: VideoReferenceMediaCapabilitiesOut | null;
  input_image_constraints?: VideoImageConstraintsOut | null;
  reference_image_constraints?: VideoImageConstraintsOut | null;
}

export interface VideoModelOptionOut {
  model: string;
  label?: string | null;
  display_name?: string | null;
  billing_model?: string | null;
  billing_models?: Partial<Record<VideoAction, string>>;
  actions: VideoAction[];
  durations_s?: number[];
  durations_by_action?: Partial<Record<VideoAction, number[]>>;
  durations_by_action_resolution?: Partial<
    Record<
      VideoAction,
      Partial<Record<VideoCreateIn["resolution"] | string, number[]>>
    >
  >;
  resolutions?: string[];
  resolutions_by_action?: Partial<Record<VideoAction, string[]>>;
  aspect_ratios?: string[];
  aspect_ratios_by_action?: Partial<Record<VideoAction, string[]>>;
  generate_audio?: boolean;
  generate_audio_by_action?: Partial<Record<VideoAction, boolean>>;
  defaults?: VideoParameterDefaultsOut;
  defaults_by_action?: Partial<Record<VideoAction, VideoParameterDefaultsOut>>;
  reference_media_limits?: Partial<
    Record<VideoReferenceMediaIn["kind"], number>
  >;
  reference_media_total_limit?: number | null;
  allow_audio_only_reference?: boolean;
  input_image_constraints?: VideoImageConstraintsOut | null;
  reference_image_constraints?: VideoImageConstraintsOut | null;
  capabilities?: Partial<Record<VideoAction, VideoActionCapabilityOut>>;
  action_capabilities?: Partial<Record<VideoAction, VideoActionCapabilityOut>>;
}

export interface VideoOptionsOut {
  enabled: boolean;
  models: VideoModelOptionOut[];
  actions?: VideoAction[];
  default_action?: VideoAction | null;
  default_model?: string | null;
  durations_s: number[];
  resolutions: string[];
  aspect_ratios: string[];
  generate_audio: boolean;
  defaults?: VideoParameterDefaultsOut;
  input_image_constraints?: VideoImageConstraintsOut | null;
  reference_image_constraints?: VideoImageConstraintsOut | null;
  pricing: VideoPriceOptionOut[];
  hold_estimates: Record<string, unknown>;
  unavailable_reason?: string | null;
}

export interface VideoGenerationOut {
  id: string;
  action: VideoAction;
  model: string;
  prompt: string;
  input_image_id?: string | null;
  reference_media: VideoReferenceMediaOut[];
  duration_s: number;
  resolution: string;
  aspect_ratio: string;
  fps?: number | null;
  generate_audio: boolean;
  seed?: number | null;
  status: VideoStatus;
  progress_stage: VideoStage;
  progress_pct: number;
  submission_epoch?: number;
  provider_name?: string | null;
  provider_kind?: string | null;
  est_token_upper: number;
  est_cost: MoneyOut;
  billed_tokens?: number | null;
  billed_cost?: MoneyOut | null;
  video?: VideoOut | null;
  temporary_download?: VideoTemporaryDownloadOut | null;
  elapsed_ms?: number | null;
  error_code?: string | null;
  error_message?: string | null;
  diagnostics?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  submit_started_at?: string | null;
  submitted_at?: string | null;
  finished_at?: string | null;
}

export interface VideoGenerationsOut {
  items: VideoGenerationOut[];
  next_cursor?: string | null;
}

export interface PricingRuleUpsertIn {
  scope: "image_size" | "chat_model" | "video";
  key: string;
  variant?: string;
  unit:
    | "per_image"
    | "per_1k_tokens_in"
    | "per_1k_tokens_out"
    | "per_1k_tokens_cache_read"
    | "per_1k_tokens_cache_creation"
    | "per_1k_tokens_cache_creation_5m"
    | "per_1k_tokens_cache_creation_1h"
    | "per_1k_tokens_image_output"
    | "per_1k_tokens_reasoning"
    | "per_1k_tokens_input_priority"
    | "per_1k_tokens_output_priority"
    | "per_1k_tokens_cache_read_priority"
    | "long_context_threshold"
    | "long_context_input_multiplier"
    | "long_context_output_multiplier"
    | "per_mtoken";
  price_rmb: string;
  priority?: number;
  enabled?: boolean;
  note?: string | null;
}

export interface AdminPricingBulkRatesIn {
  input?: string | number | null;
  output?: string | number | null;
  cache_read?: string | number | null;
  cache_creation?: string | number | null;
  cache_creation_5m?: string | number | null;
  cache_creation_1h?: string | number | null;
  image_output?: string | number | null;
  reasoning?: string | number | null;
  input_priority?: string | number | null;
  output_priority?: string | number | null;
  cache_read_priority?: string | number | null;
  long_context_threshold?: number | null;
  long_context_input_multiplier?: number | null;
  long_context_output_multiplier?: number | null;
}

export interface AdminPricingBulkIn {
  model: string;
  channel?: string | null;
  rates: AdminPricingBulkRatesIn;
  priority?: number;
  enabled?: boolean;
  note?: string | null;
}

export interface RedemptionOut {
  amount: MoneyOut;
  balance: MoneyOut;
}

export interface RedemptionUsageOut {
  id: string;
  code_id: string;
  amount: MoneyOut;
  redeemed_at: string;
}

export interface RedemptionUsageListOut {
  items: RedemptionUsageOut[];
  next_cursor?: string | null;
}

export interface AdminRedemptionCodeOut {
  id: string;
  code_prefix: string;
  amount: MoneyOut;
  max_redemptions: number;
  redeemed_count: number;
  usable_count: number;
  status: "active" | "revoked" | "expired" | "exhausted";
  batch_id: string | null;
  note: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface AdminRedemptionCodeListOut {
  items: AdminRedemptionCodeOut[];
  next_cursor?: string | null;
}

export interface AdminRedemptionUsageOut {
  id: string;
  code_id: string;
  user_id: string;
  user_email: string | null;
  amount: MoneyOut;
  wallet_tx_id: string;
  redeemed_at: string;
  ip_hash: string | null;
}

export interface AdminRedemptionUsageListOut {
  items: AdminRedemptionUsageOut[];
  next_cursor?: string | null;
}

export interface AdminRedemptionCodeCreateOut {
  batch_id: string;
  count: number;
  amount: MoneyOut;
  download_token: string;
  plaintext_codes: string[];
  expires_at: string | null;
}

export interface AdminWalletOut {
  user_id: string;
  email: string;
  account_mode: "wallet" | "byok";
  wallet: WalletOut;
  last_topup_at?: string | null;
  last_charge_at?: string | null;
}

export interface AdminWalletListOut {
  items: AdminWalletOut[];
  next_cursor?: string | null;
}

export interface AdminWalletDetailOut extends AdminWalletOut {
  last_redemption_at?: string | null;
  transactions: WalletTransactionOut[];
  redemptions: AdminRedemptionUsageOut[];
}

export interface AdminBillingAuditEventOut {
  id: string;
  event_type: string;
  user_id: string | null;
  target_user_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export interface AdminBillingOverviewOut {
  billing_enabled: boolean;
  redemption_secret_configured: boolean;
  bootstrap_completed: boolean;
  wallet_total_balance: MoneyOut;
  active_holds_count: number;
  active_holds: MoneyOut;
  codes_active: number;
  codes_redeemed_24h: number;
  codes_redeemed_24h_amount: MoneyOut;
  charges_24h: MoneyOut;
  thresholds_pricing_aligned: boolean;
  thresholds_missing_prices: string[];
  recent_audit_events: AdminBillingAuditEventOut[];
}

export interface AdminWalletAuditOut {
  ok: boolean;
  transactions: number;
  users: number;
  mismatch_count: number;
  mismatches: string[];
}

export interface AdminOrphanHoldOut {
  tx: WalletTransactionOut;
  user_id: string;
  age_seconds: number;
  recovery_action: "release" | "settle_default" | "manual_review";
}

export interface AdminBillingBootstrapIn {
  redemption_code_secret?: string | null;
  enabled?: boolean;
  usd_to_rmb_rate?: number;
  low_balance_warn_rmb?: string;
  image_size_thresholds?: Record<string, number>;
  image_prices_rmb?: Record<string, string>;
}

export interface AdminRedemptionBatchRedownloadOut {
  batch_id: string;
  count: number;
  download_token: string;
  plaintext_codes: string[];
  expires_in_seconds: number;
}
