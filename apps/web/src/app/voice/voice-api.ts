import { ApiError, apiFetch } from "@/lib/api/http";
import { getPrivateIdentitySnapshot, isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import { semanticPostRequest, withSemanticPostIdempotency } from "@/lib/api/semanticIdempotency";

export interface VoiceCapabilities {
  enabled: boolean;
  ready: boolean;
  region: string | null;
  price_micro_per_character?: number | null;
  preview_estimated_micro?: number | null;
  max_input_bytes: number;
  max_text_characters: number;
}
export interface VoiceProfile {
  id: string;
  name: string;
  state: string;
  saved: boolean;
  preview_url?: string | null;
}
export interface SpeechJob {
  id: string;
  voice_id: string;
  state: string;
  kind: string;
  segments?: { segment_no: number; state: string; revision?: number }[];
  download_url?: string | null;
  estimated_characters?: number;
  output_retrieval_failed?: boolean;
}
interface ListResponse<T> { items: T[] }
interface Quote { characters: number; estimated_micro: number; currency: "CNY" }
export type ConsentKind = "self" | "authorized";
export interface VoiceSample {
  file: File;
  name: string;
  start_ms: number;
  end_ms: number;
  consent_kind: ConsentKind;
  consent_confirmed: true;
  channel: 0 | 1;
}

const json = (data: object) => JSON.stringify(data);
const record = (value: string) => encodeURIComponent(value);
export const voiceKey = ["voice-workspace", "profiles"] as const;
export const speechKey = ["voice-workspace", "jobs"] as const;
export const voiceCapabilitiesKey = ["voice-workspace", "capabilities"] as const;

export async function getCapabilities(signal?: AbortSignal): Promise<VoiceCapabilities> {
  try {
    return await apiFetch<VoiceCapabilities>("/speech/capabilities", { signal });
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return { enabled: false, ready: false, region: null, max_input_bytes: 0, max_text_characters: 0 };
    throw error;
  }
}

export const getVoices = (signal?: AbortSignal) => apiFetch<ListResponse<VoiceProfile>>("/voices", { signal });
export const getSpeechJobs = (signal?: AbortSignal) => apiFetch<ListResponse<SpeechJob>>("/speech/jobs", { signal });
export const getSpeechQuote = (text: string, signal?: AbortSignal) =>
  apiFetch<Quote>("/speech/quote", { method: "POST", body: json({ text }), signal });

export async function createVoice(sample: VoiceSample) {
  const identity = getPrivateIdentitySnapshot();
  const { file, ...fields } = sample;
  const bytes = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  if (!isPrivateIdentitySnapshotCurrent(identity)) throw new Error("Voice upload identity changed");
  return withSemanticPostIdempotency("voice-create", { ...fields, sha256 }, (key) => {
    if (!isPrivateIdentitySnapshotCurrent(identity)) throw new Error("Voice upload identity changed");
    const body = new FormData();
    body.set("file", file);
    for (const [name, value] of Object.entries(fields)) body.set(name, String(value));
    return apiFetch<VoiceProfile>("/voices", semanticPostRequest(key, { body }));
  });
}

export function requestPreview(voiceId: string) {
  return withSemanticPostIdempotency("voice-preview", { voiceId }, (key) =>
    apiFetch<SpeechJob>(`/voices/${record(voiceId)}/preview`, semanticPostRequest(key, { body: json({}) })),
  );
}

export const saveVoice = (voiceId: string, name: string) =>
  apiFetch<VoiceProfile>(`/voices/${record(voiceId)}`, { method: "PATCH", body: json({ name, save: true }) });

export const revokeVoice = (voiceId: string) =>
  apiFetch(`/voices/${record(voiceId)}`, { method: "DELETE", allowNullResponse: true });

interface SpeechRequest { voice_id: string; text: string; rate?: number }
export function createSpeech(body: SpeechRequest) {
  return withSemanticPostIdempotency("speech-create", body, (key) =>
    apiFetch<SpeechJob>("/speech/jobs", semanticPostRequest(key, { body: json(body) })),
  );
}

export function regenerateSpeechSegment(jobId: string, segmentNo: number, revision?: number) {
  return withSemanticPostIdempotency("speech-segment-retry", { jobId, segmentNo, revision }, (key) =>
    apiFetch<SpeechJob>(`/speech/jobs/${record(jobId)}/segments/${segmentNo}/retry`, semanticPostRequest(key, { body: json({}) })),
  );
}
