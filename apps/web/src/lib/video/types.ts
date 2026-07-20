import type { VideoReferenceMediaIn } from "@/lib/types";

export type ReferenceDraft = VideoReferenceMediaIn & {
  _key: string;
  label: string;
  ref_id: string;
  display: string;
  previewUrl?: string | null;
};
