import type { VideoCreateIn, VideoGenerationOut, VideoModelOptionOut, VideoReferenceMediaOut } from "@/lib/types";

export type VideoDraftMode = "direct" | "official" | "reference";
export function isStandardSeedance25(...ids: Array<string | null | undefined>): boolean {
  return ids.some((id) => {
    const value = (id ?? "").toLowerCase();
    return /seedance[-.]2[-.]5(?:$|[-.])/.test(value) && !/flash/.test(value);
  });
}
export function supportsOfficialDraft(option?: VideoModelOptionOut): boolean {
  if (!option || /flash/i.test(`${option.model} ${option.billing_model ?? ""}`)) return false;
  return option.supports_draft ?? isStandardSeedance25(option.model, option.billing_model);
}
export function officialDraftBlockedReason(item: VideoGenerationOut, now = Date.now()): string | null {
  if (!item.draft) return "不是官方草稿";
  if (item.status !== "succeeded") return "草稿未成功";
  const expires = Date.parse(item.draft_expires_at ?? "");
  if (!Number.isFinite(expires)) return "草稿有效期未确认";
  if (expires <= now) return "草稿已过期，需重新生成";
  return null;
}
function cleanReference(ref: VideoReferenceMediaOut): VideoReferenceMediaOut {
  return {
    kind: ref.kind, image_id: ref.image_id, video_id: ref.video_id,
    url: ref.image_id || ref.video_id ? undefined : ref.url,
    label: ref.label, ref_id: ref.ref_id,
  };
}
export function officialFinalPayload(item: VideoGenerationOut): Omit<VideoCreateIn, "idempotency_key"> {
  return {
    action: item.action, model: item.model, prompt: item.prompt,
    input_image_id: item.input_image_id, reference_media: item.reference_media.map(cleanReference),
    duration_s: item.duration_s, aspect_ratio: item.aspect_ratio,
    generate_audio: item.generate_audio, seed: item.seed,
    resolution: "1080p", draft: false, draft_generation_id: item.id,
    reference_draft: false, reference_generation_id: null,
  };
}
export function referenceFinalMedia(item: VideoGenerationOut): VideoReferenceMediaOut[] {
  if (!item.video || item.status !== "succeeded") throw new Error("参考草稿视频不可用");
  const refs = item.reference_media.map(cleanReference);
  if (item.input_image_id && !refs.some((ref) => ref.image_id === item.input_image_id)) {
    refs.push({ kind: "image", image_id: item.input_image_id, label: "原首帧" });
  }
  // Preserve existing reference anchors; allocate unused IDs for appended media.
  const ids = new Set(refs.map((ref) => ref.ref_id).filter(Boolean));
  const allocate = (kind: string) => {
    let n = 1;
    while (ids.has(`ref:${kind}:${n}`)) n += 1;
    const id = `ref:${kind}:${n}`;
    ids.add(id);
    return id;
  };
  for (const ref of refs) if (!ref.ref_id) ref.ref_id = allocate(ref.kind);
  refs.push({ kind: "video", video_id: item.video.id, label: "参考草稿", ref_id: allocate("video") });
  return refs;
}
export function referenceFinalResolution(resolutions: readonly string[]): string {
  return resolutions.includes("1080p") ? "1080p" : "";
}
export function draftWorkflowLabel(item: VideoGenerationOut): string {
  if (item.draft) return "官方草稿";
  if (item.reference_draft) return "参考草稿";
  if (item.draft_generation_id) return "官方成片";
  if (item.reference_generation_id) return "参考成片";
  return "";
}
