import type { Generation } from "@/lib/types";

export function generationRenderSignature(gen: Generation | undefined): string {
  if (!gen) return "missing";
  const image = gen.image;
  return JSON.stringify([
    gen.id,
    gen.status,
    gen.stage,
    gen.substage,
    gen.retrying,
    gen.waiting_provider,
    gen.cancelled,
    gen.retryable,
    gen.attempt,
    gen.max_attempts,
    gen.retry_eta,
    gen.error_code,
    gen.error_message,
    gen.prompt,
    gen.aspect_ratio,
    gen.size_requested,
    gen.started_at,
    gen.finished_at,
    gen.failover_count,
    gen.billing_free,
    gen.billing_label,
    gen.is_dual_race_bonus,
    image?.id,
    image?.display_url,
    image?.preview_url,
    image?.thumb_url,
    image?.width,
    image?.height,
    image?.size_actual,
  ]);
}
