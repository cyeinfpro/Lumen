"use client";

import {
  Check,
  ChevronDown,
  Pencil,
  Plus,
  Save,
  Server,
  Sparkles,
  TestTube2,
} from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import type {
  ApiSupplierTemplateOut,
  ByokPurpose,
} from "@/lib/types";

import {
  clampInt,
  CONCURRENCY_MAX,
  CONCURRENCY_MIN,
  PURPOSES,
  safeHostname,
  SUPPLIER_PRESETS,
  supplierToDraft,
  TIMEOUT_MAX_MS,
  TIMEOUT_MIN_MS,
  togglePurpose,
  validateBaseUrl,
} from "./ByokPanel.model";
import type { SupplierDraft } from "./ByokPanel.model";
import { FieldNumber, FieldText, ToggleRow } from "./ByokPanel.shared";

export function NewSupplierSection({
  draft,
  urlError,
  open,
  busy,
  onToggle,
  onChange,
  onUrlBlur,
  onCreate,
}: {
  draft: SupplierDraft;
  urlError: string | null;
  open: boolean;
  busy: boolean;
  onToggle: () => void;
  onChange: (draft: SupplierDraft) => void;
  onUrlBlur: (error: string | null) => void;
  onCreate: () => void;
}) {
  return (
    <section className="surface-card space-y-4 p-5">
      <header className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 type-caption uppercase tracking-wider text-[var(--fg-2)]">
          <Plus className="w-3.5 h-3.5" />
          新供应商
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={onToggle}
          rightIcon={
            <ChevronDown
              className={
                "w-3.5 h-3.5 transition-transform " +
                (open ? "rotate-180" : "")
              }
            />
          }
        >
          {open ? "收起" : "展开表单"}
        </Button>
      </header>

      {open && (
        <div className="space-y-4">
          <div className="flex gap-2 flex-wrap">
            {SUPPLIER_PRESETS.map((preset) => (
              <Button
                key={preset.value}
                variant="secondary"
                size="sm"
                onClick={() => {
                  onChange(preset.apply());
                  onUrlBlur(null);
                }}
                leftIcon={<Sparkles className="w-3 h-3" />}
              >
                {preset.label}
              </Button>
            ))}
          </div>

          <SupplierForm
            draft={draft}
            urlError={urlError}
            onChange={onChange}
            onUrlBlur={onUrlBlur}
            showProbe={false}
          />

          <Button
            variant="primary"
            size="md"
            onClick={onCreate}
            disabled={busy}
            loading={busy}
            leftIcon={!busy ? <Server className="w-4 h-4" /> : undefined}
          >
            创建模板
          </Button>
        </div>
      )}
    </section>
  );
}

export function ByokSupplierList({
  suppliers,
  openSupplierId,
  supplierDrafts,
  supplierUrlErrors,
  probeResult,
  busy,
  onToggle,
  onChange,
  onUrlBlur,
  onSave,
  onProbe,
}: {
  suppliers: ApiSupplierTemplateOut[];
  openSupplierId: string | null;
  supplierDrafts: Record<string, SupplierDraft>;
  supplierUrlErrors: Record<string, string | null>;
  probeResult: Record<string, string>;
  busy: boolean;
  onToggle: (id: string) => void;
  onChange: (id: string, draft: SupplierDraft) => void;
  onUrlBlur: (id: string, error: string | null) => void;
  onSave: (supplier: ApiSupplierTemplateOut) => void;
  onProbe: (supplier: ApiSupplierTemplateOut) => void;
}) {
  return (
    <section className="space-y-3">
      <header className="flex items-center justify-between gap-3 px-1">
        <div className="type-caption uppercase tracking-wider text-[var(--fg-2)]">
          已有供应商 · {suppliers.length}
        </div>
      </header>
      {suppliers.length === 0 ? (
        <div className="rounded-[var(--radius-card)] border border-dashed border-[var(--border)] bg-[var(--bg-2)] py-10 text-center type-body-sm text-[var(--fg-1)]">
          还没有供应商模板，使用上方「新供应商」创建。
        </div>
      ) : (
        suppliers.map((supplier) => (
          <SupplierRow
            key={supplier.id}
            supplier={supplier}
            open={openSupplierId === supplier.id}
            onToggle={() => onToggle(supplier.id)}
            draft={supplierDrafts[supplier.id] ?? supplierToDraft(supplier)}
            urlError={supplierUrlErrors[supplier.id] ?? null}
            onChange={(draft) => onChange(supplier.id, draft)}
            onUrlBlur={(error) => onUrlBlur(supplier.id, error)}
            onSave={() => onSave(supplier)}
            onProbe={() => onProbe(supplier)}
            probeLabel={probeResult[supplier.id]}
            busy={busy}
          />
        ))
      )}
    </section>
  );
}

function SupplierRow({
  supplier,
  open,
  onToggle,
  draft,
  urlError,
  onChange,
  onUrlBlur,
  onSave,
  onProbe,
  probeLabel,
  busy,
}: {
  supplier: ApiSupplierTemplateOut;
  open: boolean;
  onToggle: () => void;
  draft: SupplierDraft;
  urlError: string | null;
  onChange: (next: SupplierDraft) => void;
  onUrlBlur: (error: string | null) => void;
  onSave: () => void;
  onProbe: () => void;
  probeLabel?: string;
  busy: boolean;
}) {
  return (
    <article className="surface-card overflow-hidden">
      <header className="flex flex-wrap items-center gap-3 px-4 py-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="type-body-sm text-[var(--fg-0)] truncate">
              {supplier.name}
            </h3>
            {supplier.enabled ? (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)] type-caption bg-success-soft text-success border border-success-border">
                <Check className="w-3 h-3" /> 启用
              </span>
            ) : (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-[var(--radius-control)] type-caption bg-[var(--bg-2)] text-[var(--fg-2)] border border-[var(--border)]">
                已禁用
              </span>
            )}
          </div>
          <p className="type-caption text-[var(--fg-2)] truncate mt-0.5">
            {safeHostname(supplier.base_url)} · {supplier.purposes.join("/")}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap type-caption">
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-2.5 py-1 text-[var(--fg-1)]">
            活跃 Key {supplier.active_credentials}
          </span>
          <span className="rounded-full border border-[var(--border)] bg-[var(--bg-2)] px-2.5 py-1 text-[var(--fg-1)]">
            验证模型 {supplier.validation_model}
          </span>
          <Button
            variant="secondary"
            size="sm"
            onClick={onToggle}
            aria-expanded={open}
            leftIcon={<Pencil className="w-3.5 h-3.5" />}
          >
            {open ? "收起" : copy.action.edit}
          </Button>
        </div>
      </header>

      {Object.keys(supplier.recent_error_counts).length > 0 && (
        <p className="px-4 pb-2 type-caption text-[var(--fg-2)]">
          近期错误：
          {Object.entries(supplier.recent_error_counts)
            .map(([key, value]) => `${key}:${value}`)
            .join(" · ")}
        </p>
      )}

      {open && (
        <div className="border-t border-[var(--border)] p-4 space-y-4 bg-[var(--bg-2)]">
          <SupplierForm
            draft={draft}
            urlError={urlError}
            onChange={onChange}
            onUrlBlur={onUrlBlur}
            showProbe
          />
          <div className="flex items-center gap-2 flex-wrap">
            <Button
              variant="primary"
              size="md"
              onClick={onSave}
              disabled={busy}
              loading={busy}
              leftIcon={!busy ? <Save className="w-4 h-4" /> : undefined}
            >
              {copy.action.save}
            </Button>
            <Button
              variant="secondary"
              size="md"
              onClick={onProbe}
              disabled={busy || !draft.probe_key.trim()}
              leftIcon={<TestTube2 className="w-4 h-4" />}
            >
              探活
            </Button>
            {probeLabel && (
              <span className="type-caption text-[var(--fg-2)]">
                {probeLabel}
              </span>
            )}
          </div>
        </div>
      )}
    </article>
  );
}

function SupplierForm({
  draft,
  urlError,
  onChange,
  onUrlBlur,
  showProbe,
}: {
  draft: SupplierDraft;
  urlError: string | null;
  onChange: (next: SupplierDraft) => void;
  onUrlBlur: (error: string | null) => void;
  showProbe: boolean;
}) {
  const set = (patch: Partial<SupplierDraft>) =>
    onChange({ ...draft, ...patch });
  return (
    <div className="space-y-4">
      <div className="space-y-3">
        <FieldText
          label="名称"
          hint="管理员后台展示的名字（如 OpenAI、SiliconFlow）"
          value={draft.name}
          onChange={(value) => set({ name: value })}
          placeholder="OpenAI"
        />
        <FieldText
          label="Base URL"
          hint="OpenAI 兼容根域名，不要带 /v1 后缀"
          value={draft.base_url}
          onChange={(value) => {
            set({ base_url: value });
            if (urlError) onUrlBlur(null);
          }}
          onBlur={(value) => onUrlBlur(validateBaseUrl(value))}
          placeholder="https://api.example.com"
          error={urlError}
        />
        <PurposesField
          purposes={draft.purposes}
          onToggle={(purpose) =>
            set({ purposes: togglePurpose(draft.purposes, purpose) })
          }
        />
        <ToggleRow
          checked={draft.enabled}
          label="启用此供应商"
          hint="禁用后用户和探活均不可使用"
          onChange={(value) => set({ enabled: value })}
        />
      </div>

      <details className="group rounded-[var(--radius-panel)] border border-[var(--border)] bg-[var(--bg-2)] overflow-hidden">
        <summary className="cursor-pointer list-none px-3 py-2 type-caption text-[var(--fg-2)] flex items-center justify-between">
          <span>高级配置</span>
          <ChevronDown className="w-3.5 h-3.5 transition-transform group-open:rotate-180" />
        </summary>
        <div className="p-3 space-y-3 border-t border-[var(--border-subtle)]">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <FieldText
              label="Slug"
              hint="可选；留空后端自动从 name 生成（仅小写英文/数字）"
              value={draft.slug ?? ""}
              onChange={(value) => set({ slug: value })}
              placeholder="auto"
            />
            <FieldText
              label="代理名 proxy_name"
              hint="可选；走 admin 已配置的 proxy 池"
              value={draft.proxy_name ?? ""}
              onChange={(value) => set({ proxy_name: value })}
              placeholder="无"
            />
            <FieldText
              label="验证模型"
              hint="探活时用的 chat model"
              value={draft.validation_model}
              onChange={(value) => set({ validation_model: value })}
              placeholder="gpt-5.4"
            />
            <FieldText
              label="默认对话模型"
              hint="该供应商下用户对话的默认 model"
              value={draft.default_chat_model}
              onChange={(value) => set({ default_chat_model: value })}
              placeholder="gpt-5.4"
            />
            <FieldText
              label="快速对话模型"
              hint="标题生成 / 上下文等轻任务使用"
              value={draft.fast_chat_model ?? ""}
              onChange={(value) => set({ fast_chat_model: value })}
              placeholder="gpt-5.4-mini"
            />
            <FieldNumber
              label="验证超时 (ms)"
              hint={`${TIMEOUT_MIN_MS}-${TIMEOUT_MAX_MS}`}
              min={TIMEOUT_MIN_MS}
              max={TIMEOUT_MAX_MS}
              value={draft.validation_timeout_ms}
              onChange={(value) =>
                set({
                  validation_timeout_ms: clampInt(
                    value,
                    TIMEOUT_MIN_MS,
                    TIMEOUT_MAX_MS,
                  ),
                })
              }
            />
            <FieldNumber
              label="text 并发 / Key"
              hint={`${CONCURRENCY_MIN}-${CONCURRENCY_MAX}`}
              min={CONCURRENCY_MIN}
              max={CONCURRENCY_MAX}
              value={draft.text_concurrency_per_key}
              onChange={(value) =>
                set({
                  text_concurrency_per_key: clampInt(
                    value,
                    CONCURRENCY_MIN,
                    CONCURRENCY_MAX,
                  ),
                })
              }
            />
            <FieldNumber
              label="image 并发 / Key"
              hint={`${CONCURRENCY_MIN}-${CONCURRENCY_MAX}`}
              min={CONCURRENCY_MIN}
              max={CONCURRENCY_MAX}
              value={draft.image_concurrency_per_key}
              onChange={(value) =>
                set({
                  image_concurrency_per_key: clampInt(
                    value,
                    CONCURRENCY_MIN,
                    CONCURRENCY_MAX,
                  ),
                })
              }
            />
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3 pt-2 border-t border-[var(--border-subtle)]">
            <ToggleRow
              checked={draft.public_signup_enabled}
              label="允许公开注册使用"
              hint="该供应商可被未登录用户在 BYOK 注册流程中选择"
              onChange={(value) => set({ public_signup_enabled: value })}
            />
            <ToggleRow
              checked={draft.user_bind_enabled}
              label="允许已登录用户绑定"
              hint="该供应商出现在账号设置 → API Key 列表中"
              onChange={(value) => set({ user_bind_enabled: value })}
            />
          </div>
        </div>
      </details>

      {showProbe && (
        <FieldText
          label="探活 Key"
          hint="临时填一个用户 Key，仅用本次探活，不会保存到后端"
          value={draft.probe_key}
          onChange={(value) => set({ probe_key: value })}
          placeholder="sk-..."
          isPassword
        />
      )}
    </div>
  );
}

function PurposesField({
  purposes,
  onToggle,
}: {
  purposes: ByokPurpose[];
  onToggle: (purpose: ByokPurpose) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="type-caption uppercase tracking-wider text-[var(--fg-1)]">
        用途
      </span>
      <div className="flex gap-2 flex-wrap">
        {PURPOSES.map((purpose) => {
          const active = purposes.includes(purpose.value);
          return (
            <button
              key={purpose.value}
              type="button"
              onClick={() => onToggle(purpose.value)}
              className={
                "px-2.5 py-1 rounded-[var(--radius-card)] border type-caption transition-colors " +
                (active
                  ? "bg-[var(--accent)] text-black border-[var(--accent)]"
                  : "bg-[var(--bg-2)] text-[var(--fg-1)] border-[var(--border)] hover:bg-[var(--bg-3)]")
              }
            >
              {purpose.label}
            </button>
          );
        })}
      </div>
      <span className="type-caption text-[var(--fg-2)]">
        该供应商支持的模型类型，影响下游路由（至少选 1 个）
      </span>
    </div>
  );
}
