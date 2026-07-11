import type { VideoOut } from "../types";
import { apiFetch } from "./http";

export interface StoryboardAsset {
  id: string;
  kind: "character" | "scene" | "prop" | string;
  name: string;
  role: string;
  description: string;
  continuity: string;
  revision: number;
  status: string;
  prompt: string;
  image_id?: string | null;
  image_url?: string | null;
  display_url?: string | null;
  generation_id?: string | null;
  approved_at?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface StoryboardShot {
  id: string;
  index: number;
  title: string;
  purpose: string;
  narration: string;
  visual: string;
  shot_type: string;
  camera_move: string;
  transition: string;
  reference_notes: string;
  duration_s: number;
  asset_ids: string[];
  keyframe_prompt: string;
  keyframe_source_hash?: string | null;
  current_source_hash: string;
  keyframe_stale: boolean;
  status: string;
  keyframe_image_id?: string | null;
  keyframe_image_url?: string | null;
  keyframe_display_url?: string | null;
  keyframe_generation_id?: string | null;
  keyframe_approved_at?: string | null;
  video_generation_id?: string | null;
  video?: VideoOut | null;
  video_status?: string | null;
  video_progress_stage?: string | null;
  video_progress_pct?: number | null;
  error_code?: string | null;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
}

export interface StoryboardAssembly {
  status: string;
  video_id?: string | null;
  video_url?: string | null;
  poster_url?: string | null;
  segment_count: number;
  segment_ids: string[];
  error_code?: string | null;
  error_message?: string | null;
  updated_at?: string | null;
}

export interface StoryboardRun {
  id: string;
  conversation_id?: string | null;
  title: string;
  idea: string;
  style: string;
  script: string;
  script_confirmed: boolean;
  script_revision: number;
  aspect_ratio: string;
  resolution: string;
  model: string;
  generate_audio: boolean;
  seed?: number | null;
  status: string;
  current_stage: string;
  assets: StoryboardAsset[];
  shots: StoryboardShot[];
  assembly?: StoryboardAssembly | null;
  thumbnail_url?: string | null;
  created_at: string;
  updated_at: string;
}

export interface StoryboardListItem {
  id: string;
  title: string;
  idea: string;
  status: string;
  current_stage: string;
  asset_count: number;
  approved_asset_count: number;
  shot_count: number;
  done_shot_count: number;
  thumbnail_url?: string | null;
  created_at: string;
  updated_at: string;
}

export interface StoryboardListResponse {
  items: StoryboardListItem[];
  next_cursor?: string | null;
}

export interface StoryboardCreateIn {
  title: string;
  idea: string;
  style?: string;
  aspect_ratio?: string;
  resolution?: string;
  model?: string;
  generate_audio?: boolean;
  seed?: number | null;
}

export interface StoryboardPatchIn {
  title?: string;
  idea?: string;
  style?: string;
  script?: string;
  script_confirmed?: boolean;
  aspect_ratio?: string;
  resolution?: string;
  model?: string;
  generate_audio?: boolean;
  seed?: number | null;
  current_stage?: string;
}

export interface StoryboardAssetCreateIn {
  kind: "character" | "scene" | "prop";
  name: string;
  role?: string;
  description?: string;
  continuity?: string;
}

export type StoryboardAssetPatchIn = Partial<StoryboardAssetCreateIn>;

export interface StoryboardShotCreateIn {
  title?: string;
  purpose?: string;
  narration?: string;
  visual?: string;
  shot_type?: string;
  camera_move?: string;
  transition?: string;
  reference_notes?: string;
  duration_s?: number;
  asset_ids?: string[];
  keyframe_prompt?: string;
}

export type StoryboardShotPatchIn = StoryboardShotCreateIn;

export interface StoryboardGenerateIn {
  prompt?: string | null;
}

export interface StoryboardSubmitShotIn {
  prompt?: string | null;
  duration_s?: number | null;
  idempotency_key?: string | null;
}

export function listStoryboards(
  opts: { cursor?: string | null; limit?: number } = {},
): Promise<StoryboardListResponse> {
  const qs = new URLSearchParams();
  if (opts.cursor) qs.set("cursor", opts.cursor);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return apiFetch<StoryboardListResponse>(`/storyboards${suffix}`);
}

export function createStoryboard(
  body: StoryboardCreateIn,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>("/storyboards", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getStoryboard(id: string): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(`/storyboards/${encodeURIComponent(id)}`);
}

export function patchStoryboard(
  id: string,
  body: StoryboardPatchIn,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(`/storyboards/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function createStoryboardAsset(
  storyboardId: string,
  body: StoryboardAssetCreateIn,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/assets`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export function generateStoryboardAsset(
  storyboardId: string,
  stepId: string,
  body: StoryboardGenerateIn = {},
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/assets/${encodeURIComponent(stepId)}/generate`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function approveStoryboardAsset(
  storyboardId: string,
  stepId: string,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/assets/${encodeURIComponent(stepId)}/approve`,
    { method: "POST" },
  );
}

export function deleteStoryboardAsset(
  storyboardId: string,
  stepId: string,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/assets/${encodeURIComponent(stepId)}`,
    { method: "DELETE" },
  );
}

export function rebuildStoryboardShots(
  storyboardId: string,
  body: { shots?: StoryboardShotCreateIn[] | null; replace?: boolean } = {},
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots/rebuild`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function createStoryboardShot(
  storyboardId: string,
  body: StoryboardShotCreateIn,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export function patchStoryboardShot(
  storyboardId: string,
  stepId: string,
  body: StoryboardShotPatchIn,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots/${encodeURIComponent(stepId)}`,
    { method: "PATCH", body: JSON.stringify(body) },
  );
}

export function approveStoryboardShot(
  storyboardId: string,
  stepId: string,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots/${encodeURIComponent(stepId)}/approve`,
    { method: "POST" },
  );
}

export function moveStoryboardShot(
  storyboardId: string,
  stepId: string,
  direction: -1 | 1,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots/${encodeURIComponent(stepId)}/move`,
    { method: "POST", body: JSON.stringify({ direction }) },
  );
}

export function deleteStoryboardShot(
  storyboardId: string,
  stepId: string,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots/${encodeURIComponent(stepId)}`,
    { method: "DELETE" },
  );
}

export function generateStoryboardKeyframe(
  storyboardId: string,
  stepId: string,
  body: StoryboardGenerateIn = {},
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots/${encodeURIComponent(stepId)}/keyframe`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function generateAllStoryboardKeyframes(
  storyboardId: string,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots/keyframes/generate-all`,
    { method: "POST" },
  );
}

export function approveStoryboardKeyframe(
  storyboardId: string,
  stepId: string,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots/${encodeURIComponent(stepId)}/keyframe/approve`,
    { method: "POST" },
  );
}

export function submitStoryboardShot(
  storyboardId: string,
  stepId: string,
  body: StoryboardSubmitShotIn = {},
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots/${encodeURIComponent(stepId)}/submit`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function submitAllStoryboardShots(
  storyboardId: string,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/shots/submit-all`,
    { method: "POST" },
  );
}

export function assembleStoryboard(
  storyboardId: string,
): Promise<StoryboardRun> {
  return apiFetch<StoryboardRun>(
    `/storyboards/${encodeURIComponent(storyboardId)}/assemble`,
    { method: "POST" },
  );
}
