"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Button, Input, Select } from "@/components/ui/primitives";
import type { ConsentKind, VoiceSample } from "./voice-api";

const MAX_SELECTION_SECONDS = 20;
const MIN_SELECTION_SECONDS = 3;
const MAX_SOURCE_DURATION_SECONDS = 60;
const MIME_SUFFIX = /\.(wav|mp3|m4a)$/i;

type Channel = 0 | 1;
type SampleAnalysis = { buffer: AudioBuffer; duration: number; channels: number; rate: number };
type ReadySample = SampleAnalysis & { file: File; channel: Channel; warning: string | null };
type ClipPlayback = { context: AudioContext; source: AudioBufferSourceNode; splitter?: ChannelSplitterNode };
type Range = { start: number; end: number };

export function VoiceCapture({ maxBytes, busy, submitting, onCreate }: {
  maxBytes: number;
  busy: boolean;
  submitting: boolean;
  onCreate: (sample: VoiceSample) => Promise<boolean>;
}) {
  const channelId = useId();
  const [sample, setSample] = useState<ReadySample | null>(null);
  const [range, setRange] = useState<Range>({ start: 0, end: 0 });
  const [name, setName] = useState("");
  const [kind, setKind] = useState<ConsentKind>("self");
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);
  const [playing, setPlaying] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const epoch = useRef(0);
  const playback = useRef<ClipPlayback | null>(null);

  useEffect(() => () => {
    epoch.current += 1;
    closeClip(playback.current);
    playback.current = null;
  }, []);

  function clear() {
    epoch.current += 1;
    stopClip();
    setChecking(false);
    setSample(null);
    setConfirmed(false);
    setError("");
    if (input.current) input.current.value = "";
  }

  async function choose(file?: File) {
    if (!file || busy) return;
    clear();
    if (!MIME_SUFFIX.test(file.name) || !file.size || file.size > maxBytes) {
      setError(`请选择不超过 ${Math.floor(maxBytes / 1_000_000)} MB 的 WAV、MP3 或 M4A。`);
      return;
    }
    const ticket = epoch.current;
    setChecking(true);
    try {
      const analysis = await analyze(file);
      if (ticket !== epoch.current) return;
      setSample(selectChannel({ file, ...analysis }, 0));
      setRange({ start: 0, end: Math.min(Math.floor(analysis.duration * 1000) / 1000, MAX_SELECTION_SECONDS) });
    } catch (err) {
      if (ticket === epoch.current) setError(err instanceof Error ? err.message : "无法读取音频。");
    } finally {
      if (ticket === epoch.current) setChecking(false);
    }
  }

  const startMs = Math.round(range.start * 1000);
  const endMs = Math.round(range.end * 1000);
  const validRange = isValidRange(startMs, endMs, sample?.duration);
  const canSubmit = canSubmitSample(validRange, busy, checking, confirmed, name);

  function stopClip() {
    const current = playback.current;
    playback.current = null;
    setPlaying(false);
    closeClip(current);
  }

  async function playClip() {
    if (!sample || !validRange) return;
    stopClip();
    let current: ClipPlayback | null = null;
    try {
      current = prepareClip(sample);
      playback.current = current;
      current.source.onended = () => {
        if (playback.current === current) stopClip();
      };
      setPlaying(true);
      await current.context.resume();
      if (playback.current === current) current.source.start(0, startMs / 1000, (endMs - startMs) / 1000);
    } catch {
      if (playback.current !== current) return;
      stopClip();
      setError("无法播放选段，请换一份录音。");
    }
  }

  function updateRange(next: Range) {
    stopClip();
    if (range.start !== next.start || range.end !== next.end) setConfirmed(false);
    setRange(next);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!sample || !canSubmit) return;
    const ok = await onCreate({
      file: sample.file,
      name: name.trim(),
      start_ms: startMs,
      end_ms: endMs,
      consent_kind: kind,
      consent_confirmed: true,
      channel: sample.channel,
    });
    if (ok) { clear(); setName(""); }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="grid gap-4">
      <div>
        <h2 className="type-section-title font-semibold">录入音色</h2>
        <p className="mt-1 type-body-sm text-[var(--fg-muted-aa)]">用一段清楚的单人干声开始。建议选择 10–20 秒，不要加音乐。</p>
      </div>
      <div
        className="grid min-w-0 gap-2 rounded-[var(--radius-card)] border border-dashed border-[var(--border-strong)] bg-[var(--bg-0)] p-4"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => { event.preventDefault(); void choose(event.dataTransfer.files.item(0) ?? undefined); }}
      >
        <label className="grid gap-2 type-body-sm font-medium">
          上传或拖入录音
          <input
            ref={input}
            type="file"
            accept=".wav,.mp3,.m4a,audio/wav,audio/mpeg,audio/mp4"
            disabled={busy}
            onChange={(event) => void choose(event.currentTarget.files?.[0])}
            className="w-full min-w-0 type-body-sm text-[var(--fg-1)]"
          />
        </label>
        {checking && <div className="flex flex-wrap items-center gap-2">
          <p role="status" className="type-body-sm">正在检查录音…</p>
          <Button variant="secondary" size="sm" type="button" disabled={submitting} onClick={clear}>取消检查</Button>
        </div>}
        {error && <p role="alert" className="type-body-sm text-[var(--danger-fg)]">{error}</p>}
        {sample && (
          <div className="grid gap-3 min-w-0">
            <p className="break-all type-body-sm text-[var(--fg-muted-aa)]">
              {sample.file.name} · {sample.duration.toFixed(1)} 秒 · {sample.channels} 声道 · {Math.round(sample.rate / 1000)} kHz（浏览器解码）
            </p>
            {sample.warning && <p className="type-body-sm text-[var(--fg-muted-aa)]">{sample.warning}</p>}
            {sample.channels > 1 && (
              <div className="grid gap-1 type-body-sm">
                <label htmlFor={channelId}>选择人声所在声道</label>
                <Select id={channelId} value={sample.channel} disabled={busy}
                  className="min-h-11"
                  onChange={(event) => {
                    stopClip();
                    setConfirmed(false);
                    setSample(selectChannel(sample, event.currentTarget.value === "1" ? 1 : 0));
                  }}>
                  <option value="0">第一声道（左）</option>
                  <option value="1">第二声道（右）</option>
                </Select>
              </div>
            )}
            <div className="grid grid-cols-2 gap-3">
              <label className="grid gap-1 type-body-sm">开始（秒）
                <Input type="number" step={0.001} min={0} max={Math.floor(sample.duration * 1000) / 1000} disabled={submitting} value={range.start} onChange={(event) =>
                  updateRange({ ...range, start: event.currentTarget.valueAsNumber || 0 })} />
              </label>
              <label className="grid gap-1 type-body-sm">结束（秒）
                <Input type="number" step={0.001} min={0} max={Math.floor(sample.duration * 1000) / 1000} disabled={submitting} value={range.end} onChange={(event) =>
                  updateRange({ ...range, end: event.currentTarget.valueAsNumber || 0 })} />
              </label>
            </div>
            {!validRange && <p className="type-body-sm text-[var(--danger-fg)]">请选择 3–20 秒的有效区间。</p>}
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" size="sm" type="button" disabled={!validRange}
                onClick={() => { if (playing) stopClip(); else void playClip(); }}>
                {playing ? "停止试听" : "试听选段"}
              </Button>
              <Button variant="secondary" size="sm" type="button" disabled={busy} onClick={clear}>移除录音</Button>
            </div>
          </div>
        )}
      </div>
      <label className="grid gap-1 type-body-sm">音色名称
        <Input required maxLength={80} disabled={submitting} value={name} onChange={(event) => setName(event.target.value)} placeholder="例如：纪录片旁白" />
      </label>
      <fieldset disabled={submitting} className="grid gap-2 type-body-sm">
        <legend className="mb-2 font-medium">录音授权</legend>
        <label className="flex min-h-11 items-center gap-2">
          <input type="radio" name="voice-permission" checked={kind === "self"} onChange={() => { setKind("self"); setConfirmed(false); }} /> 我的声音
        </label>
        <label className="flex min-h-11 items-center gap-2">
          <input type="radio" name="voice-permission" checked={kind === "authorized"} onChange={() => { setKind("authorized"); setConfirmed(false); }} /> 已得到说话人明确授权
        </label>
        <label className="flex min-h-11 items-start gap-2 text-[var(--fg-1)]">
          <input className="mt-1" type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
          我确认录音只有一位说话人，并同意将选段发送至阿里百炼用于创建私有音色。
        </label>
      </fieldset>
      <Button variant="primary" type="submit" disabled={!canSubmit}>
        创建音色
      </Button>
    </form>
  );
}

function isValidRange(start: number, end: number, duration?: number) {
  const length = end - start;
  return duration !== undefined && start >= 0 && end <= Math.floor(duration * 1000) &&
    length >= MIN_SELECTION_SECONDS * 1000 && length <= MAX_SELECTION_SECONDS * 1000;
}

function canSubmitSample(range: boolean, busy: boolean, checking: boolean, consent: boolean, name: string) {
  return range && !busy && !checking && consent && name.trim().length > 0;
}

async function analyze(file: File): Promise<SampleAnalysis> {
  const context = new AudioContext();
  try {
    const decoded = await context.decodeAudioData(await file.arrayBuffer());
    const { duration, numberOfChannels, sampleRate } = decoded;
    if (duration < MIN_SELECTION_SECONDS || duration > MAX_SOURCE_DURATION_SECONDS + 0.001) {
      throw new Error("参考录音须为 3–60 秒；太长请先在剪辑软件里裁短。");
    }
    return { buffer: decoded, duration, channels: numberOfChannels, rate: sampleRate };
  } finally {
    await context.close().catch(() => {});
  }
}

function checkQuality(samples: Float32Array): string | null {
  let clipped = 0;
  let peak = 0;
  for (const value of samples) {
    const absolute = Math.abs(value);
    peak = Math.max(peak, absolute);
    if (absolute >= 0.99) clipped++;
  }
  if (peak < 0.03) return "这个声道的声音很小，试听后可换声道或重新选录音。";
  if (clipped > samples.length / 100) return "这个声道可能有爆音，建议换一段没有削波的素材。";
  return null;
}

function selectChannel(sample: SampleAnalysis & { file: File }, channel: Channel): ReadySample {
  return { ...sample, channel, warning: checkQuality(sample.buffer.getChannelData(channel)) };
}

// Play the selected decoded channel through Web Audio rather than a blob URL.
// This stays local, preserves the original sample for server normalization, and
// works under Lumen's existing CSP without adding a media-source exception.
function prepareClip(sample: ReadySample): ClipPlayback {
  const context = new AudioContext();
  try {
    const source = context.createBufferSource();
    source.buffer = sample.buffer;
    if (sample.channels < 2) {
      source.connect(context.destination);
      return { context, source };
    }
    // Split before the destination can downmix stereo. Play only the channel
    // selected for server-side extraction, without allocating another clip.
    const splitter = context.createChannelSplitter(sample.channels);
    source.connect(splitter);
    splitter.connect(context.destination, sample.channel, 0);
    return { context, source, splitter };
  } catch (error) {
    void context.close().catch(() => {});
    throw error;
  }
}

function closeClip(clip: ClipPlayback | null) {
  if (!clip) return;
  clip.source.onended = null;
  clip.source.disconnect();
  clip.splitter?.disconnect();
  // Closing the context also stops a running or not-yet-started source.
  void clip.context.close().catch(() => {});
}
