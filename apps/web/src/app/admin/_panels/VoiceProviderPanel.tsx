"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AudioLines } from "lucide-react";

import { Button, Select } from "@/components/ui/primitives";
import { apiFetch } from "@/lib/api/http";
import { USER_QUERY_SCOPE } from "@/lib/queries/userScope";
import {
  BEIJING,
  SINGAPORE,
  voiceProviderPatch,
  voiceProviderStatusLabel,
  type VoiceProviderDraft,
  type VoiceProviderStatus,
  type VoiceRegion,
} from "./VoiceProviderPanel.model";

const PATH = "/admin/voice-provider";
const QUERY_KEY = ["admin", "voice-provider"] as const;
const fieldClass =
  "mt-1 h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-[var(--fg-0)] placeholder:text-[var(--fg-2)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent";

export function VoiceProviderPanel() {
  const cache = useQueryClient();
  const regionId = useId();
  const keyField = useRef<HTMLInputElement>(null);
  const writing = useRef(false);
  const [busy, setBusy] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [draft, setDraft] = useState<VoiceProviderDraft>({});
  const [message, setMessage] = useState<{ error: boolean; text: string } | null>(null);

  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: ({ signal }) => apiFetch<VoiceProviderStatus>(PATH, { signal, cache: "no-store" }),
    retry: false,
    refetchInterval: ({ state: { data } }) => data?.configured && !data.ready ? 5_000 : false,
    refetchIntervalInBackground: false,
  });
  const status = query.data;
  const configured = status?.configured;
  const ready = status?.ready;
  useEffect(() => {
    if (configured === undefined || ready === undefined) return;
    void cache.invalidateQueries({
      predicate: ({ queryKey }) => {
        const scopeOffset = queryKey[0] === USER_QUERY_SCOPE ? 2 : 0;
        return queryKey[scopeOffset] === "voice-workspace" && queryKey[scopeOffset + 1] === "capabilities";
      },
    });
  }, [cache, configured, ready]);

  async function persist(method: "PUT" | "DELETE", body?: string) {
    if (writing.current) return;
    writing.current = true;
    setBusy(true);
    setMessage(null);
    try {
      await cache.cancelQueries({ queryKey: QUERY_KEY });
      const next = await apiFetch<VoiceProviderStatus>(PATH, { method, body, cache: "no-store" });
      cache.setQueryData(QUERY_KEY, next);
      setDraft({});
      setConfirmRemove(false);
      setMessage({
        error: false,
        text: method === "DELETE" ? "Key 已移除，已停止接收新的声音任务。" :
          next.ready ? "已保存，声音功能可以使用。" : "已保存，服务就绪后自动启用。",
      });
    } catch {
      // Do not echo request/error details: an input validation error may carry the submitted key.
      setMessage({ error: true, text: "未能确认变更结果。请刷新状态，核对后再操作。" });
      void query.refetch();
    } finally {
      writing.current = false;
      setBusy(false);
    }
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const field = keyField.current;
    if (!status || !field || writing.current) return;
    const patch = voiceProviderPatch(status, draft, field.value);
    if ("error" in patch) {
      setMessage({ error: true, text: patch.error });
      return;
    }
    if (!Object.keys(patch.body).length) {
      setMessage({ error: false, text: "没有需要保存的变更。" });
      return;
    }
    const body = JSON.stringify(patch.body);
    field.value = "";
    void persist("PUT", body);
  }

  return (
    <section aria-labelledby="voice-provider-heading" className="surface-card-v2 space-y-4 rounded-[var(--radius-card)] border border-[var(--border)] p-4 sm:p-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <AudioLines aria-hidden="true" className="size-5 shrink-0 text-accent" />
          <div>
            <h3 id="voice-provider-heading" className="type-card-title">声音 · 阿里云百炼</h3>
            <p className="mt-0.5 type-caption text-[var(--fg-2)]">保存百炼 Key 后自动启用音色复刻与配音，无需重启。</p>
          </div>
        </div>
        {status && (
          <span role="status" className={status.ready ? "type-caption text-success" : "type-caption text-[var(--fg-2)]"}>
            {voiceProviderStatusLabel(status)}
          </span>
        )}
      </header>

      {query.isPending && <p role="status" className="type-caption text-[var(--fg-2)]">正在读取配置…</p>}
      {query.isError && (
        <div className="flex flex-wrap items-center gap-2">
          <p role="alert" className="type-caption text-[var(--danger-fg)]">无法读取最新配置。</p>
          <Button type="button" variant="secondary" size="sm" onClick={() => void query.refetch()} loading={query.isFetching}>
            重新读取
          </Button>
        </div>
      )}

      {status && !query.isError && (
        <form onSubmit={save} className="space-y-3" aria-busy={busy}>
          <fieldset className="space-y-3" disabled={busy || confirmRemove}>
            <label className="block type-control text-[var(--fg-1)]">
              阿里云百炼 API Key
              <input
                ref={keyField}
                className={fieldClass}
                type="password"
                autoComplete="off"
                spellCheck={false}
                maxLength={512}
                placeholder={status.configured ? "已配置 · 输入新 Key 可替换" : "粘贴百炼 API Key"}
              />
            </label>
            <details className="rounded-[var(--radius-control)] border border-[var(--border-subtle)] p-3">
              <summary className="cursor-pointer type-control text-[var(--fg-1)]">地区与工作空间（可选）</summary>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <div>
                  <label htmlFor={regionId} className="block type-control text-[var(--fg-1)]">
                    Key 所在地区
                  </label>
                  <Select
                    id={regionId}
                    wrapperClassName="mt-1"
                    value={draft.region ?? status.region}
                    onChange={(event) => setDraft({ ...draft, region: event.currentTarget.value as VoiceRegion })}
                  >
                    <option value={BEIJING}>北京</option>
                    <option value={SINGAPORE}>新加坡</option>
                  </Select>
                </div>
                <label className="block type-control text-[var(--fg-1)]">
                  Workspace ID
                  <input
                    className={fieldClass}
                    value={draft.workspace ?? status.workspace_id ?? ""}
                    onChange={(event) => setDraft({ ...draft, workspace: event.target.value })}
                    maxLength={63}
                    placeholder="留空使用默认工作空间"
                  />
                </label>
              </div>
              <p className="mt-2 type-caption text-[var(--fg-2)]">默认北京。地区或工作空间变更时，需要重新填写对应的 Key。</p>
            </details>
          </fieldset>

          {!confirmRemove ? (
            <div className="flex flex-wrap gap-2">
              <Button type="submit" variant="primary" size="sm" loading={busy}>保存 Key</Button>
              {status.configured && (
                <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmRemove(true)} disabled={busy}>
                  移除 Key
                </Button>
              )}
            </div>
          ) : (
            <div className="space-y-2 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft p-3">
              <p className="type-caption text-[var(--danger-fg)]">移除后将停止接收新的声音任务。确定移除吗？</p>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button" variant="danger" size="sm" loading={busy}
                  onClick={() => {
                    if (keyField.current) keyField.current.value = "";
                    void persist("DELETE");
                  }}
                >确定移除</Button>
                <Button type="button" variant="secondary" size="sm" disabled={busy} onClick={() => setConfirmRemove(false)}>
                  取消
                </Button>
              </div>
            </div>
          )}
        </form>
      )}
      {message && (
        <p role={message.error ? "alert" : "status"} className={message.error ? "type-caption text-[var(--danger-fg)]" : "type-caption text-[var(--fg-1)]"}>
          {message.text}
        </p>
      )}
    </section>
  );
}
