"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Spinner, Textarea } from "@/components/ui/primitives";
import { DesktopTopNav, MobileTabBar } from "@/components/ui/shell";
import { useIsMobile } from "@/hooks/useMediaQuery";
import { isPrivateIdentitySnapshotCurrent } from "@/lib/auth/privateIdentityEpoch";
import { userScopedQueryKey } from "@/lib/queries/userScope";
import { useChatStore } from "@/store/useChatStore";
import { useUiStore } from "@/store/useUiStore";
import {
  createSpeech, createVoice, getCapabilities, getSpeechJobs, getSpeechQuote, getVoices,
  regenerateSpeechSegment, requestPreview, revokeVoice,
  saveVoice, speechKey, voiceCapabilitiesKey, voiceKey, type VoiceSample,
} from "./voice-api";
import { draftAfterAcceptedSubmission, hasUnknownOutcome, isInProgress, yuan } from "./voice-display";
import { VoiceCapture } from "./voice-capture";
import { SpeechHistory, VoiceLibrary } from "./voice-history";

const PANEL = "grid min-w-0 content-start gap-5 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-1)] p-4 md:p-5";
const EMPTY_INPUT_BYTES = 10_000_000;
const EMPTY_INPUT_CHARACTERS = 10_000;
const POLL_ACTIVE_MS = 2_000;
const POLL_UNCERTAIN_MS = 15_000;

export function VoiceWorkspace() {
  const userId = useChatStore((state) => state.currentUserId);
  const epoch = useUiStore((state) => state.lightbox.identityEpoch);
  const identity = { userId, epoch };
  const accountReady = isPrivateIdentitySnapshotCurrent(identity);
  return <UserVoiceWorkspace key={JSON.stringify([userId, epoch])} identity={identity} accountReady={accountReady} />;
}

type Identity = Parameters<typeof isPrivateIdentitySnapshotCurrent>[0];

function useVoiceWorkspace(identity: Identity, accountReady: boolean) {
  const { userId, epoch } = identity;
  const scopedCapabilities = userScopedQueryKey(userId, [...voiceCapabilitiesKey, epoch]);
  const scopedVoices = userScopedQueryKey(userId, [...voiceKey, epoch]);
  const scopedJobs = userScopedQueryKey(userId, [...speechKey, epoch]);
  const client = useQueryClient();
  async function forCurrentIdentity<T>(read: () => Promise<T>) {
    if (!isPrivateIdentitySnapshotCurrent(identity)) throw new Error("Voice identity changed");
    const result = await read();
    if (!isPrivateIdentitySnapshotCurrent(identity)) throw new Error("Voice identity changed");
    return result;
  }
  const capabilities = useQuery({
    queryKey: scopedCapabilities,
    queryFn: ({ signal }) => forCurrentIdentity(() => getCapabilities(signal)),
    enabled: accountReady, retry: false, staleTime: 30_000,
    refetchInterval: (query) => query.state.data?.enabled && !query.state.data.ready ? 30_000 : false,
  });
  const enabled = accountReady && capabilities.data?.enabled === true;
  const ready = enabled && !capabilities.isError && capabilities.data?.ready === true;
  const voices = useQuery({
    queryKey: scopedVoices, queryFn: ({ signal }) => forCurrentIdentity(() => getVoices(signal)), enabled, retry: false,
    refetchInterval: (query) => pollingFor(query.state.data?.items),
  });
  const jobs = useQuery({
    queryKey: scopedJobs, queryFn: ({ signal }) => forCurrentIdentity(() => getSpeechJobs(signal)), enabled, retry: false,
    refetchInterval: (query) => pollingFor(query.state.data?.items),
  });
  const [active, setActive] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ error: boolean; text: string } | null>(null);
  const guard = useRef(false);

  async function run(label: string, action: () => Promise<unknown>, accepted: string) {
    if (guard.current || !enabled || !isPrivateIdentitySnapshotCurrent(identity)) return false;
    guard.current = true;
    setActive(label);
    setNotice(null);
    try {
      await action();
      setNotice({ error: false, text: accepted });
      return true;
    } catch (error) {
      setNotice({ error: true, text: safeError(error) });
      return false;
    } finally {
      guard.current = false;
      setActive(null);
      if (isPrivateIdentitySnapshotCurrent(identity)) refresh();
    }
  }

  function refresh() {
    if (!isPrivateIdentitySnapshotCurrent(identity)) return;
    void client.invalidateQueries({ queryKey: scopedVoices });
    void client.invalidateQueries({ queryKey: scopedJobs });
  }
  return { capabilities, enabled, ready, voices, jobs, active, notice, run, refresh, identity };
}

function UserVoiceWorkspace({ identity, accountReady }: { identity: Identity; accountReady: boolean }) {
  const mobile = useIsMobile();
  const model = useVoiceWorkspace(identity, accountReady);
  const { capabilities, enabled, refresh } = model;
  function refreshWorkspace() {
    if (!isPrivateIdentitySnapshotCurrent(identity)) return;
    void capabilities.refetch();
    refresh();
  }
  return (
    <div data-app-viewport className="flex min-h-[100dvh] w-full flex-col bg-[var(--bg-0)] text-[var(--fg-0)]">
      {mobile === false && <DesktopTopNav active="me" />}
      <main data-app-scroll className="mx-auto grid w-full max-w-[var(--content-workbench)] grow content-start gap-6 mobile-page-gutter mobile-bottom-clearance pt-5 md:px-6 md:py-8">
        <header className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="type-page-title font-semibold">声音</h1>
            <p className="mt-2 type-body-sm text-[var(--fg-muted-aa)]">录入私有音色，用一段文稿制作可剪辑的 WAV 旁白。</p>
          </div>
          {enabled && <Button size="sm" onClick={refreshWorkspace}>刷新任务</Button>}
        </header>
        <WorkspaceGate
          accountReady={accountReady}
          loading={accountReady && capabilities.isPending}
          unavailable={capabilities.isError}
          enabled={enabled}
          onRefresh={refreshWorkspace}
        />
        {enabled && <WorkspaceContent model={model} />}
      </main>
      {mobile === true && <MobileTabBar />}
    </div>
  );
}

function WorkspaceContent({ model }: { model: ReturnType<typeof useVoiceWorkspace> }) {
  const { capabilities, ready, voices, jobs, active, run } = model;
  const profiles = voices.data?.items ?? [];
  const speechJobs = jobs.data?.items ?? [];
  const busy = active !== null || !ready;
  const pendingPreviews = new Map<string, "pending" | "unknown">();
  // Unknown takes precedence over active; do not prompt users to resubmit an
  // earlier preview whose charge or outcome has not been reconciled.
  for (const job of speechJobs) {
    if (job.kind !== "preview") continue;
    if (hasUnknownOutcome(job.state)) pendingPreviews.set(job.voice_id, "unknown");
    else if (isInProgress(job.state) && !pendingPreviews.has(job.voice_id)) pendingPreviews.set(job.voice_id, "pending");
  }
  const previewProfiles = profiles.map((voice) => ({ ...voice, previewPending: pendingPreviews.get(voice.id) }));
  return (
    <>
      <WorkspaceNotices model={model} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className={PANEL}>
          <VoiceCapture
            maxBytes={capabilities.data?.max_input_bytes ?? EMPTY_INPUT_BYTES}
            busy={busy}
            submitting={active === "create"}
            onCreate={(sample: VoiceSample) => run("create", () => createVoice(sample), "录音已提交。音色准备好后可生成试听。")}
          />
          {voices.isPending && <p role="status" className="type-body-sm text-[var(--fg-muted-aa)]">正在读取音色…</p>}
          {voices.data && (
            <VoiceLibrary voices={previewProfiles} busy={active} canGenerate={ready} previewPrice={capabilities.data?.preview_estimated_micro}
              onPreview={(id) => run("preview", () => requestPreview(id), "试听已排队。")}
              onSave={(id, name) => run("save", () => saveVoice(id, name), "已保存到我的音色。")}
              onRevoke={(id) => run("revoke", () => revokeVoice(id), "已提交撤销请求，等待服务确认。")} />
          )}
        </div>
        <div className={PANEL}>
          <SpeechComposer
            voices={profiles.filter((voice) => voice.saved && voice.state === "ready")}
            maxText={capabilities.data?.max_text_characters ?? EMPTY_INPUT_CHARACTERS}
            identity={model.identity}
            busy={busy}
            onCreate={(voice_id, text, rate) => run("speech", () => createSpeech({ voice_id, text, rate }), "配音已排队，可以离开页面。")}
          />
          {jobs.isPending && <p role="status" className="type-body-sm text-[var(--fg-muted-aa)]">正在读取配音任务…</p>}
          {jobs.data && <SpeechHistory
            jobs={speechJobs.filter((job) => job.kind !== "preview")}
            busy={busy}
            onRetry={(id, n, revision) => run("segment", () => regenerateSpeechSegment(id, n, revision), "该段已提交重做。")}
          />}
        </div>
      </div>
    </>
  );
}

function WorkspaceNotices({ model }: { model: ReturnType<typeof useVoiceWorkspace> }) {
  const { ready, voices, jobs, notice } = model;
  return <>
    {!ready && <p role="status" className="type-body-sm text-[var(--fg-muted-aa)]">声音生成暂不可用，已有音色和成品仍可查看。</p>}
    {notice && <p role={notice.error ? "alert" : "status"} className={notice.error ? "type-body-sm text-[var(--danger-fg)]" : "type-body-sm text-[var(--fg-1)]"}>{notice.text}</p>}
    {(voices.isError || jobs.isError) && <p role="alert" className="type-body-sm text-[var(--danger-fg)]">音色或历史暂时没有更新，点击「刷新任务」再试。</p>}
  </>;
}

function WorkspaceGate({ accountReady, loading, unavailable, enabled, onRefresh }: {
  accountReady: boolean; loading: boolean; unavailable: boolean; enabled: boolean; onRefresh: () => void;
}) {
  if (enabled) return null;
  if (!accountReady) return <p role="status" className="type-body-sm">登录后可使用声音工作台。</p>;
  if (loading) return <p role="status" className="flex items-center gap-2 type-body-sm"><Spinner size={16} />加载声音工作台…</p>;
  return (
    <div className={PANEL}>
      <p role={unavailable ? "alert" : "status"} className="type-body-sm">
        {unavailable ? "暂时无法读取声音服务状态。" : "声音工作台尚未对这个账号开放。"}
      </p>
      {unavailable && <Button size="sm" className="w-fit" onClick={onRefresh}>重试</Button>}
    </div>
  );
}

function SpeechComposer({ voices, maxText, busy, identity, onCreate }: {
  voices: { id: string; name: string }[];
  maxText: number;
  busy: boolean;
  identity: Identity;
  onCreate: (id: string, text: string, rate?: number) => Promise<boolean>;
}) {
  const [selection, setSelection] = useState("");
  const [text, setText] = useState("");
  const [rate, setRate] = useState(1);
  const voiceId = voices.find((voice) => voice.id === selection)?.id ?? voices[0]?.id ?? "";
  const cleaned = text.trim();
  const quoteText = voiceId && cleaned.length <= maxText ? cleaned : "";
  const quote = useQuote(quoteText, identity);
  const allowed = canGenerateSpeech(busy, quote.value, rate);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!allowed) return;
    const submittedDraft = text;
    if (await onCreate(voiceId, cleaned, rate)) {
      setText((currentDraft) => draftAfterAcceptedSubmission(currentDraft, submittedDraft));
    }
  }
  return (
    <form onSubmit={(event) => void submit(event)} className="grid gap-4">
      <div>
        <h2 className="type-section-title font-semibold">制作配音</h2>
        <p className="mt-1 type-body-sm text-[var(--fg-muted-aa)]">选一个已保存的音色，粘贴要说的内容。</p>
      </div>
      <label className="grid gap-1 type-body-sm">音色
        <select className="min-h-11 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-[var(--fg-0)]"
          value={voiceId} onChange={(event) => setSelection(event.target.value)} disabled={!voices.length || busy}>
          {voices.length === 0 && <option value="">先试听并保存一个音色</option>}
          {voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}
        </select>
      </label>
      <label className="grid gap-1 type-body-sm">文稿
        <Textarea rows={7} maxLength={maxText} value={text} onChange={(event) => setText(event.target.value)} placeholder="在这里粘贴需要配音的文字…" />
      </label>
      <details>
        <summary className="min-h-11 cursor-pointer py-3 type-body-sm">调整语速</summary>
        <label className="grid gap-2 type-body-sm">语速 · {rate.toFixed(2)} 倍
          <input type="range" min={0.5} max={2} step={0.05} value={rate} onChange={(event) => setRate(event.target.valueAsNumber)} />
        </label>
      </details>
      <p role="status" className="min-h-5 type-body-sm text-[var(--fg-muted-aa)]">
        {quoteLabel(quoteText, quote)}
      </p>
      {quote.failed && <Button size="sm" className="w-fit" onClick={quote.retry}>重新估算</Button>}
      <Button variant="primary" type="submit" disabled={!allowed}>生成配音</Button>
    </form>
  );
}

type QuoteValue = Awaited<ReturnType<typeof getSpeechQuote>>;
type QuoteDisplay = { value?: QuoteValue; failed?: boolean };

function quoteLabel(text: string, quote: QuoteDisplay) {
  if (!text) return "合成按计费字符收取，输出音频不另收费。";
  if (quote.value) return `${quote.value.characters} 计费字符 · 预计 ${yuan(quote.value.estimated_micro)}`;
  return quote.failed ? "无法计算费用，请检查稿件后重试。" : "正在计算费用…";
}

function canGenerateSpeech(busy: boolean, quote: QuoteValue | undefined, rate: number) {
  return !busy && quote !== undefined && Number.isFinite(rate) && rate >= 0.5 && rate <= 2;
}

function useQuote(text: string, { userId, epoch }: Identity) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<(QuoteDisplay & { text: string; attempt: number })>();
  const visible = result?.text === text && result.attempt === attempt ? result : undefined;
  useEffect(() => {
    if (!text) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      const ownsQuote = () => !controller.signal.aborted && isPrivateIdentitySnapshotCurrent({ userId, epoch });
      if (!ownsQuote()) return;
      getSpeechQuote(text, controller.signal).then(
        (value) => { if (ownsQuote()) setResult({ text, value, attempt }); },
        () => { if (ownsQuote()) setResult({ text, failed: true, attempt }); },
      );
    }, 350);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [text, attempt, userId, epoch]);
  return { ...visible, retry: () => setAttempt((value) => value + 1) };
}

function pollingFor(items: { state: string }[] | undefined): number | false {
  if (items?.some((item) => isInProgress(item.state))) return POLL_ACTIVE_MS;
  if (items?.some((item) => hasUnknownOutcome(item.state))) return POLL_UNCERTAIN_MS;
  return false;
}

function safeError(err: unknown) {
  if (err && typeof err === "object" && "status" in err) {
    if (err.status === 401 || err.status === 403) return "账号暂时无法执行此操作，请检查登录状态或功能权限。";
    if (err.status === 429) return "请求较多，请稍后重试。";
    if (err.status === 402) return "账户余额不足。";
    if (err.status === 409) return "任务状态已变化或同一请求正在处理。请先刷新任务。";
    if (err.status === 413) return "录音或请求超出大小限制，请缩短后再试。";
    if (err.status === 503) return "声音服务暂不可用，稍后刷新再试。";
    if (err.status === 422) return "提交内容不符合要求，请检查录音或文字。";
  }
  return "操作未确认。请先刷新任务核对状态，避免重复提交。";
}
