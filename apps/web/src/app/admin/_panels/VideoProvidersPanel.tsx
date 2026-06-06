"use client";

import { useMemo, useState } from "react";
import {
  AlertCircle,
  Check,
  Clapperboard,
  Pencil,
  Plus,
  Save,
  Trash2,
  X,
} from "lucide-react";

import {
  useUpdateVideoProvidersMutation,
  useVideoProvidersQuery,
} from "@/lib/queries";
import { ApiError } from "@/lib/apiClient";
import type {
  VideoProviderItemIn,
  VideoProviderItemOut,
  VideoProviderKind,
} from "@/lib/types";
import { Button, IconButton } from "@/components/ui/primitives";
import { ErrorBlock } from "../page";

type ModelDraft = {
  _key: number;
  model: string;
  t2v: string;
  i2v: string;
  reference: string;
};

type Draft = {
  _key: number;
  name: string;
  kind: VideoProviderKind;
  base_url: string;
  api_key: string;
  enabled: boolean;
  priority: number;
  weight: number;
  concurrency: number;
  proxy: string;
  models: ModelDraft[];
};

let seq = 0;
function nextKey() {
  seq += 1;
  return seq;
}

function modelsToRows(models: Record<string, string>): ModelDraft[] {
  const rows = new Map<string, ModelDraft>();
  const rowFor = (model: string) => {
    const existing = rows.get(model);
    if (existing) return existing;
    const next = {
      _key: nextKey(),
      model,
      t2v: "",
      i2v: "",
      reference: "",
    };
    rows.set(model, next);
    return next;
  };
  for (const [key, value] of Object.entries(models)) {
    const trimmedKey = key.trim();
    const trimmedValue = value.trim();
    if (!trimmedKey || !trimmedValue) continue;
    const [model, action] = trimmedKey.includes(":")
      ? trimmedKey.split(/:(?=[^:]+$)/)
      : [trimmedKey, ""];
    const row = rowFor(model);
    if (action === "t2v") row.t2v = trimmedValue;
    else if (action === "i2v") row.i2v = trimmedValue;
    else if (action === "reference") row.reference = trimmedValue;
    else {
      row.t2v = trimmedValue;
      row.i2v = trimmedValue;
      row.reference = trimmedValue;
    }
  }
  const out = Array.from(rows.values());
  return out.length > 0 ? out : [emptyModelDraft()];
}

function rowsToModels(rows: ModelDraft[]): Record<string, string> {
  const models: Record<string, string> = {};
  for (const row of rows) {
    const model = row.model.trim();
    if (!model) continue;
    if (row.t2v.trim()) models[`${model}:t2v`] = row.t2v.trim();
    if (row.i2v.trim()) models[`${model}:i2v`] = row.i2v.trim();
    if (row.reference.trim()) models[`${model}:reference`] = row.reference.trim();
  }
  return models;
}

function emptyModelDraft(): ModelDraft {
  return {
    _key: nextKey(),
    model: "seedance-2.0",
    t2v: "doubao-seedance-2-0-260128",
    i2v: "doubao-seedance-2-0-260128",
    reference: "doubao-seedance-2-0-260128",
  };
}

function toDraft(item: VideoProviderItemOut): Draft {
  return {
    _key: nextKey(),
    name: item.name,
    kind: item.kind,
    base_url: item.base_url,
    api_key: "",
    enabled: item.enabled,
    priority: item.priority,
    weight: item.weight,
    concurrency: item.concurrency,
    proxy: item.proxy ?? "",
    models: modelsToRows(item.models),
  };
}

function emptyDraft(): Draft {
  return {
    _key: nextKey(),
    name: "volcano-main",
    kind: "volcano",
    base_url: "https://ark.cn-beijing.volces.com/api/v3",
    api_key: "",
    enabled: true,
    priority: 100,
    weight: 1,
    concurrency: 1,
    proxy: "",
    models: [emptyModelDraft()],
  };
}

function hasStoredKey(
  serverItems: VideoProviderItemOut[],
  providerName: string,
): boolean {
  return Boolean(
    serverItems.find((item) => item.name === providerName)?.api_key_hint?.trim(),
  );
}

function saveError(err: Error): string {
  if (err instanceof ApiError) {
    return err.message || `保存失败 (HTTP ${err.status})`;
  }
  return err.message || "保存失败";
}

export function VideoProvidersPanel() {
  const query = useVideoProvidersQuery();
  const updateMut = useUpdateVideoProvidersMutation();
  const [drafts, setDrafts] = useState<Draft[] | null>(null);
  const [enabledDraft, setEnabledDraft] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const serverItems = useMemo(() => query.data?.items ?? [], [query.data?.items]);
  const proxyOptions = useMemo(
    () => query.data?.proxies ?? [],
    [query.data?.proxies],
  );
  const editing = drafts !== null;

  const enabledCount = useMemo(
    () => serverItems.filter((item) => item.enabled).length,
    [serverItems],
  );

  const startEdit = () => {
    setDrafts(serverItems.map(toDraft));
    setEnabledDraft(Boolean(query.data?.enabled));
    setError(null);
    setSaved(false);
  };

  const updateDraft = (idx: number, patch: Partial<Draft>) => {
    setDrafts((prev) => {
      if (!prev) return prev;
      const next = [...prev];
      next[idx] = { ...next[idx], ...patch };
      return next;
    });
  };

  const updateModel = (
    providerIdx: number,
    modelIdx: number,
    patch: Partial<ModelDraft>,
  ) => {
    setDrafts((prev) => {
      if (!prev) return prev;
      const next = [...prev];
      const models = [...next[providerIdx].models];
      models[modelIdx] = { ...models[modelIdx], ...patch };
      next[providerIdx] = { ...next[providerIdx], models };
      return next;
    });
  };

  const save = () => {
    if (!drafts) return;
    setError(null);
    if (enabledDraft && drafts.length === 0) {
      setError("开启视频生成前至少添加一个视频供应商");
      return;
    }
    const names = drafts.map((item) => item.name.trim());
    const duplicate = names.find((name, idx) => name && names.indexOf(name) !== idx);
    if (duplicate) {
      setError(`供应商名称重复：${duplicate}`);
      return;
    }
    const items: VideoProviderItemIn[] = [];
    for (const draft of drafts) {
      const name = draft.name.trim();
      if (!name) {
        setError("视频供应商名称不能为空");
        return;
      }
      if (!draft.base_url.trim()) {
        setError(`「${name}」缺少基础地址`);
        return;
      }
      try {
        const url = new URL(draft.base_url.trim());
        if (!["http:", "https:"].includes(url.protocol)) throw new Error("bad url");
      } catch {
        setError(`「${name}」基础地址格式不合法`);
        return;
      }
      if (draft.enabled && draft.kind !== "fake" && !draft.api_key.trim() && !hasStoredKey(serverItems, name)) {
        setError(`「${name}」缺少 API Key`);
        return;
      }
      const models = rowsToModels(draft.models);
      if (Object.keys(models).length === 0) {
        setError(`「${name}」至少需要一个模型映射`);
        return;
      }
      items.push({
        name,
        kind: draft.kind,
        base_url: draft.base_url.trim(),
        ...(draft.api_key.trim() ? { api_key: draft.api_key.trim() } : {}),
        enabled: draft.enabled,
        priority: Number(draft.priority) || 0,
        weight: Math.max(1, Number(draft.weight) || 1),
        concurrency: Math.max(1, Math.min(32, Number(draft.concurrency) || 1)),
        proxy: draft.proxy.trim() || null,
        models,
      });
    }
    updateMut.mutate(
      { enabled: enabledDraft, items },
      {
        onSuccess: () => {
          setDrafts(null);
          setSaved(true);
        },
        onError: (err) => setError(saveError(err)),
      },
    );
  };

  if (query.isLoading) {
    return (
      <section className="space-y-4">
        <div className="h-28 animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-1)]" />
        <div className="h-44 animate-pulse rounded-[var(--radius-panel)] bg-[var(--bg-1)]" />
      </section>
    );
  }

  if (query.isError) {
    return (
      <ErrorBlock
        message={query.error?.message ?? "加载失败"}
        onRetry={() => void query.refetch()}
      />
    );
  }

  return (
    <section className="space-y-5 pb-20">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[var(--radius-panel)] border border-accent-border bg-accent-soft">
            <Clapperboard className="h-4 w-4 text-accent" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-medium text-[var(--fg-0)]">AI 视频供应商</h3>
            <p className="type-caption text-[var(--fg-2)]">
              Seedance 任务 API · 文字/首帧/参考生成
            </p>
          </div>
        </div>
        {!editing && (
          <Button
            variant="primary"
            size="sm"
            onClick={startEdit}
            leftIcon={<Pencil className="h-3.5 w-3.5" />}
          >
            编辑
          </Button>
        )}
      </div>

      {error && (
        <div className="flex items-start gap-2 rounded-[var(--radius-card)] border border-danger-border bg-danger-soft px-4 py-3 type-body-sm text-danger">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="flex-1">{error}</span>
          <IconButton
            variant="ghost"
            size="sm"
            aria-label="关闭"
            onClick={() => setError(null)}
          >
            <X className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      )}
      {saved && (
        <div className="flex items-center gap-2 rounded-[var(--radius-card)] border border-success-border bg-success-soft px-4 py-3 type-body-sm text-success">
          <Check className="h-4 w-4" />
          已保存
        </div>
      )}

      {!editing ? (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <Stat label="总开关" value={query.data?.enabled ? "已开启" : "已关闭"} />
            <Stat label="启用供应商" value={`${enabledCount} / ${serverItems.length}`} />
            <Stat label="配置来源" value={query.data?.source ?? "none"} />
          </div>
          <div className="space-y-3">
            {serverItems.map((item) => (
              <div
                key={item.name}
                className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)]"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-[var(--fg-0)]">{item.name}</p>
                    <p className="mt-1 text-xs text-[var(--fg-2)] [overflow-wrap:anywhere]">
                      {item.kind} · {item.base_url}
                    </p>
                  </div>
                  <span className="rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-xs text-[var(--fg-1)]">
                    {item.enabled ? "启用" : "停用"} · 并发 {item.concurrency}
                  </span>
                </div>
                <div className="mt-3 grid gap-2 text-xs text-[var(--fg-2)] sm:grid-cols-2">
                  <span>Key：{item.api_key_hint || "未保存"}</span>
                  <span>代理：{item.proxy || "直连"}</span>
                </div>
                <div className="mt-3 grid gap-2 md:grid-cols-3">
                  {Object.entries(item.models).map(([key, value]) => (
                    <div
                      key={key}
                      className="rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)] px-3 py-2"
                    >
                      <p className="font-mono text-[11px] text-[var(--fg-1)] [overflow-wrap:anywhere]">
                        {key}
                      </p>
                      <p className="mt-1 font-mono text-[11px] text-[var(--fg-2)] [overflow-wrap:anywhere]">
                        {value}
                      </p>
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {serverItems.length === 0 && (
              <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-8 text-center text-sm text-[var(--fg-2)]">
                还没有 AI 视频供应商
              </div>
            )}
          </div>
        </>
      ) : (
        <div className="space-y-4">
          <label className="flex items-center justify-between rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 px-4 py-3 text-sm">
            <span>启用视频生成</span>
            <input
              type="checkbox"
              checked={enabledDraft}
              onChange={(event) => setEnabledDraft(event.target.checked)}
            />
          </label>
          {drafts.map((draft, idx) => (
            <ProviderEditor
              key={draft._key}
              draft={draft}
              proxies={proxyOptions.map((item) => item.name)}
              onPatch={(patch) => updateDraft(idx, patch)}
              onDelete={() => setDrafts((prev) => prev?.filter((_, i) => i !== idx) ?? null)}
              onAddModel={() =>
                updateDraft(idx, { models: [...draft.models, emptyModelDraft()] })
              }
              onPatchModel={(modelIdx, patch) => updateModel(idx, modelIdx, patch)}
              onDeleteModel={(modelIdx) =>
                updateDraft(idx, {
                  models: draft.models.filter((_, i) => i !== modelIdx),
                })
              }
            />
          ))}
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setDrafts((prev) => [...(prev ?? []), emptyDraft()])}
              leftIcon={<Plus className="h-3.5 w-3.5" />}
            >
              添加供应商
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={save}
              loading={updateMut.isPending}
              leftIcon={<Save className="h-3.5 w-3.5" />}
            >
              保存
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setDrafts(null)}
            >
              取消
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4">
      <p className="type-caption text-[var(--fg-2)]">{label}</p>
      <p className="mt-1 text-lg font-semibold text-[var(--fg-0)]">{value}</p>
    </div>
  );
}

function ProviderEditor({
  draft,
  proxies,
  onPatch,
  onDelete,
  onAddModel,
  onPatchModel,
  onDeleteModel,
}: {
  draft: Draft;
  proxies: string[];
  onPatch: (patch: Partial<Draft>) => void;
  onDelete: () => void;
  onAddModel: () => void;
  onPatchModel: (idx: number, patch: Partial<ModelDraft>) => void;
  onDeleteModel: (idx: number) => void;
}) {
  return (
    <div className="rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-1)]/60 p-4 shadow-[var(--shadow-1)]">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm font-medium text-[var(--fg-0)]">供应商</p>
        <IconButton variant="ghost" size="sm" aria-label="删除供应商" onClick={onDelete}>
          <Trash2 className="h-4 w-4" />
        </IconButton>
      </div>
      <div className="mt-4 grid gap-3 md:grid-cols-2">
        <Field label="名称" value={draft.name} onChange={(name) => onPatch({ name })} />
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">类型</span>
          <select
            value={draft.kind}
            onChange={(event) => onPatch({ kind: event.target.value as VideoProviderKind })}
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          >
            <option value="volcano">火山方舟</option>
            <option value="veo">Google Veo</option>
            <option value="fake">测试</option>
          </select>
        </label>
        <Field
          label="Base URL"
          value={draft.base_url}
          onChange={(base_url) => onPatch({ base_url })}
        />
        <Field
          label="API Key"
          value={draft.api_key}
          onChange={(api_key) => onPatch({ api_key })}
          placeholder="留空则保留已保存 Key"
          type="password"
        />
        <Field
          label="优先级"
          value={String(draft.priority)}
          onChange={(value) => onPatch({ priority: Number(value) || 0 })}
          type="number"
        />
        <Field
          label="权重"
          value={String(draft.weight)}
          onChange={(value) => onPatch({ weight: Number(value) || 1 })}
          type="number"
        />
        <Field
          label="并发"
          value={String(draft.concurrency)}
          onChange={(value) => onPatch({ concurrency: Number(value) || 1 })}
          type="number"
        />
        <label className="space-y-1.5">
          <span className="type-caption text-[var(--fg-2)]">代理</span>
          <select
            value={draft.proxy}
            onChange={(event) => onPatch({ proxy: event.target.value })}
            className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
          >
            <option value="">直连</option>
            {proxies.map((proxy) => (
              <option key={proxy} value={proxy}>
                {proxy}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="mt-3 flex items-center justify-between rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 py-2 text-sm">
        <span>启用此供应商</span>
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(event) => onPatch({ enabled: event.target.checked })}
        />
      </label>
      <div className="mt-4 space-y-3">
        <div className="flex items-center justify-between gap-2">
          <p className="type-caption text-[var(--fg-2)]">模型映射</p>
          <Button
            variant="outline"
            size="sm"
            onClick={onAddModel}
            leftIcon={<Plus className="h-3.5 w-3.5" />}
          >
            添加模型
          </Button>
        </div>
        {draft.models.map((model, idx) => (
          <div
            key={model._key}
            className="grid gap-2 rounded-[var(--radius-card)] border border-[var(--border-subtle)] bg-[var(--bg-0)] p-3 md:grid-cols-[1fr_1fr_1fr_1fr_auto]"
          >
            <Field
              label="业务模型"
              value={model.model}
              onChange={(value) => onPatchModel(idx, { model: value })}
            />
            <Field
              label="文字生成"
              value={model.t2v}
              onChange={(value) => onPatchModel(idx, { t2v: value })}
            />
            <Field
              label="首帧生成"
              value={model.i2v}
              onChange={(value) => onPatchModel(idx, { i2v: value })}
            />
            <Field
              label="参考生成"
              value={model.reference}
              onChange={(value) => onPatchModel(idx, { reference: value })}
            />
            <div className="flex items-end">
              <IconButton
                variant="ghost"
                size="sm"
                aria-label="删除模型映射"
                onClick={() => onDeleteModel(idx)}
              >
                <Trash2 className="h-4 w-4" />
              </IconButton>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <label className="space-y-1.5">
      <span className="type-caption text-[var(--fg-2)]">{label}</span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-0)] px-3 text-sm outline-none focus:border-[var(--accent)]/50"
      />
    </label>
  );
}
