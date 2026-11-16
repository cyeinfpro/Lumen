import { deepEqual, equal, throws } from "node:assert/strict";
import { test } from "node:test";
import type { VideoGenerationOut, VideoModelOptionOut } from "@/lib/types";
const url = new URL("./video-draft-mode.ts", import.meta.url);
const { supportsOfficialDraft, officialDraftBlockedReason, officialFinalPayload, referenceFinalMedia, draftWorkflowLabel, referenceFinalResolution } = await import(url.href) as typeof import("./video-draft-mode");
const task = (overrides: Partial<VideoGenerationOut> = {}) => ({
  id: "draft-1", status: "succeeded", model: "seedance-2.5", action: "reference",
  prompt: "保留原描述", reference_media: [], input_image_id: null, duration_s: 5,
  aspect_ratio: "16:9", generate_audio: true, seed: 7, resolution: "480p",
  draft: true, draft_expires_at: "2030-01-01T00:00:00Z", ...overrides,
}) as VideoGenerationOut;
test("reference final prefers supported 1080p and otherwise uses model default", () => {
  equal(referenceFinalResolution(["480p", "1080p"]), "1080p");
  equal(referenceFinalResolution(["480p", "720p"]), "");
  equal(referenceFinalResolution([]), "");
});
test("official capability respects provider denial and excludes Flash", () => {
  equal(supportsOfficialDraft({model:"seedance-2.5", supports_draft:true} as VideoModelOptionOut), true);
  equal(supportsOfficialDraft({model:"seedance-2.5", supports_draft:false} as VideoModelOptionOut), false);
  equal(supportsOfficialDraft({model:"seedance-2.5-flash", supports_draft:true} as VideoModelOptionOut), false);
});
test("official conversion blocks failed, unknown expiry and expired drafts", () => {
  equal(officialDraftBlockedReason(task(), Date.parse("2029-01-01")), null);
  equal(officialDraftBlockedReason(task(), Date.parse("2031-01-01")), "草稿已过期，需重新生成");
  equal(officialDraftBlockedReason(task({draft_expires_at:null})), "草稿有效期未确认");
  equal(officialDraftBlockedReason(task({status:"failed"})), "草稿未成功");
});
test("official conversion points to source and requests 1080p", () => {
  const payload = officialFinalPayload(task({reference_media:[{kind:"video",video_id:"source",url:"https://example.invalid/preview",mime:"video/mp4"}]}));
  equal(payload.reference_media?.[0].url, undefined);
  equal("mime" in (payload.reference_media?.[0] ?? {}), false);
  equal(payload.reference_draft,false);
  equal(payload.reference_generation_id,null);
  equal(payload.draft_generation_id, "draft-1");
  equal(payload.draft, false);
  equal(payload.resolution, "1080p");
  equal(payload.prompt, "保留原描述");
});
test("reference conversion preserves original materials and allocates fresh anchors", () => {
  const source = task({draft:false,reference_draft:true,input_image_id:"image-1",
    reference_media:[{kind:"video",video_id:"old",ref_id:"ref:video:1",mime:"video/mp4",url:"https://example.invalid/old"}],
    video:{id:"preview"} as VideoGenerationOut["video"]});
  const refs=referenceFinalMedia(source);
  equal(refs.length,3);
  equal(refs[0].video_id,"old");
  equal(refs[0].url,undefined);
  equal("mime" in refs[0],false);
  equal(refs[1].image_id,"image-1");
  equal(refs[2].video_id,"preview");
  equal(refs[2].ref_id,"ref:video:2");
  equal(source.reference_media.length,1);
  equal(draftWorkflowLabel(source),"参考草稿");
  deepEqual(new Set(refs.map(x=>x.ref_id)).size,3);
  throws(()=>referenceFinalMedia(task()), /不可用/);
});
