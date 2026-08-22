"use client";

import { AlertCircle, Check } from "lucide-react";
import type { ReactNode, RefObject } from "react";
import { Input, Select } from "@/components/ui/primitives";
import type { ProviderProxyOut } from "@/lib/types";
import {
  PROVIDER_PURPOSES,
  normalizePurposes,
  type Draft,
  type FieldErrors,
} from "./model";

export function ProviderIdentityFields({
  draft,
  proxies,
  errors,
  isExisting,
  hasExistingKey,
  nameRef,
  onUpdate,
  onDiscoverModels,
}: {
  draft: Draft;
  proxies: ProviderProxyOut[];
  errors?: FieldErrors;
  isExisting: boolean;
  hasExistingKey: boolean;
  nameRef: RefObject<HTMLInputElement | null>;
  onUpdate: (patch: Partial<Draft>) => void;
  onDiscoverModels: () => void;
}) {
  return (
    <>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <IdentityField
          label="名称"
          required
          error={errors?.name}
          hint="唯一标识"
        >
          <Input
            ref={nameRef}
            type="text"
            value={draft.name}
            onChange={(event) => onUpdate({ name: event.target.value })}
            placeholder="例如：主供应商"
            className={identityFieldClass(Boolean(errors?.name))}
          />
        </IdentityField>
        <IdentityField
          label="基础地址"
          required
          error={errors?.base_url}
          hint="支持 HTTP/HTTPS，可填内网地址"
        >
          <Input
            type="url"
            value={draft.base_url}
            onChange={(event) => onUpdate({ base_url: event.target.value })}
            onBlur={onDiscoverModels}
            placeholder="http://10.0.0.8:8000/v1"
            className={identityFieldClass(Boolean(errors?.base_url))}
          />
        </IdentityField>
      </div>

      <IdentityField
        label="API 密钥"
        hint={providerApiKeyHint(isExisting, hasExistingKey)}
        required={!isExisting || !hasExistingKey}
      >
        <Input
          type="password"
          value={draft.api_key}
          onChange={(event) => onUpdate({ api_key: event.target.value })}
          onBlur={onDiscoverModels}
          placeholder={providerApiKeyPlaceholder(isExisting, hasExistingKey)}
          autoComplete="new-password"
          className={identityFieldClass(false)}
        />
      </IdentityField>

      <PurposeField draft={draft} onUpdate={onUpdate} />

      <IdentityField label="代理" hint="供应商可直连或使用一个代理">
        <Select
          value={draft.proxy ?? ""}
          onChange={(event) => onUpdate({ proxy: event.target.value || null })}
          className={identityFieldClass(false)}
        >
          <option value="">不使用代理</option>
          {proxies.map((proxy) => (
            <option
              key={proxy.name}
              value={proxy.name.trim()}
              disabled={!proxy.name.trim()}
            >
              {proxy.name.trim() || "(未命名代理)"} ·{" "}
              {proxy.type === "ssh" ? "SSH" : "S5"}
            </option>
          ))}
        </Select>
      </IdentityField>
    </>
  );
}

function PurposeField({
  draft,
  onUpdate,
}: {
  draft: Draft;
  onUpdate: (patch: Partial<Draft>) => void;
}) {
  const purposes = normalizePurposes(draft.purposes);
  return (
    <IdentityField label="用途" hint="先按用途过滤，再按健康度与权重选号">
      <div className="flex flex-wrap gap-2">
        {PROVIDER_PURPOSES.map((option) => {
          const checked = purposes.includes(option.value);
          const disabled = checked && purposes.length === 1;
          return (
            <button
              key={option.value}
              type="button"
              disabled={disabled}
              onClick={() => {
                const next = checked
                  ? purposes.filter((item) => item !== option.value)
                  : [...purposes, option.value];
                if (next.length > 0) onUpdate({ purposes: next });
              }}
              className={
                "inline-flex min-h-[36px] items-center gap-2 rounded-[var(--radius-panel)] border px-3 type-caption transition-colors disabled:cursor-not-allowed disabled:opacity-50 " +
                (checked
                  ? "border-accent-border bg-accent-soft text-accent"
                  : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)] hover:text-[var(--fg-1)]")
              }
            >
              <span
                className={
                  "flex h-3.5 w-3.5 items-center justify-center rounded border " +
                  (checked
                    ? "border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-on)]"
                    : "border-[var(--border-strong)]")
                }
                aria-hidden
              >
                {checked ? <Check className="h-3 w-3" /> : null}
              </span>
              {option.label}
            </button>
          );
        })}
      </div>
    </IdentityField>
  );
}

function providerApiKeyHint(
  isExisting: boolean,
  hasExistingKey: boolean,
): string {
  if (!isExisting) return "新增供应商必须填写";
  return hasExistingKey
    ? "留空保持原值不变"
    : "当前没有保存密钥，启用前必须填写";
}

function providerApiKeyPlaceholder(
  isExisting: boolean,
  hasExistingKey: boolean,
): string {
  return isExisting && hasExistingKey ? "（留空保持不变）" : "sk-...";
}

function identityFieldClass(hasError: boolean): string {
  const base = "font-mono";
  if (hasError) {
    return `${base} border-danger-border focus:border-danger-border focus:ring-danger/25`;
  }
  return `${base} border-[var(--border)] focus:border-accent-border focus:ring-accent/20`;
}

function IdentityField({
  label,
  hint,
  required,
  error,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  error?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline gap-1.5">
        <span className="type-caption font-medium text-[var(--fg-1)]">
          {label}
          {required ? <span className="ml-0.5 text-danger">*</span> : null}
        </span>
        <IdentityFieldMessage hint={hint} error={error} />
      </div>
      {children}
    </div>
  );
}

function IdentityFieldMessage({
  hint,
  error,
}: {
  hint?: string;
  error?: string;
}) {
  if (error) {
    return (
      <span
        role="alert"
        className="flex items-center gap-0.5 type-caption text-danger"
      >
        <AlertCircle className="h-3 w-3" /> {error}
      </span>
    );
  }
  if (!hint) return null;
  return <span className="type-caption text-[var(--fg-2)]">{hint}</span>;
}
