"use client";

import {
  forwardRef,
  useEffect,
  useRef,
  type ReactNode,
  type RefObject,
} from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronUp,
  GripVertical,
  ImageIcon,
  Power,
  PowerOff,
  Trash2,
} from "lucide-react";
import type { ProviderProxyOut } from "@/lib/types";
import { Button } from "@/components/ui/primitives";
import { copy } from "@/lib/copy";
import {
  PROVIDER_PURPOSES,
  type Draft,
  type FieldErrors,
  endpointDisplayLabel,
  normalizePurposes,
  purposeLabel,
} from "./model";

export type DraftCardProps = {
  draft: Draft;
  proxies: ProviderProxyOut[];
  index: number;
  total: number;
  expanded: boolean;
  showDeleteConfirm: boolean;
  errors?: FieldErrors;
  isExisting: boolean;
  hasExistingKey: boolean;
  onToggle: () => void;
  onUpdate: (patch: Partial<Draft>) => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
  onDeleteConfirm: (show: boolean) => void;
};

export const DraftCard = forwardRef<HTMLDivElement, DraftCardProps>(
  function DraftCard(
    {
      draft,
      proxies,
      index,
      total,
      expanded,
      showDeleteConfirm,
      errors,
      isExisting,
      hasExistingKey,
      onToggle,
      onUpdate,
      onRemove,
      onMove,
      onDeleteConfirm,
    },
    ref,
  ) {
    const hasErrors = Boolean(errors && Object.keys(errors).length > 0);
    const nameRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
      if (expanded && !draft.name) {
        setTimeout(() => nameRef.current?.focus(), 100);
      }
    }, [expanded, draft.name]);

    return (
      <motion.div
        ref={ref}
        layout="position"
        transition={{ duration: 0.18 }}
        className={
          "overflow-hidden rounded-[var(--radius-dialog)] border backdrop-blur-sm transition-colors " +
          (expanded
            ? hasErrors
              ? "border-danger-border bg-danger-soft"
              : "border-[var(--color-lumen-amber)]/45 bg-[var(--color-lumen-amber)]/[0.04]"
            : "border-[var(--border)] bg-[var(--bg-1)]/60")
        }
      >
        <DraftCardSummary
          draft={draft}
          index={index}
          expanded={expanded}
          hasErrors={hasErrors}
          isExisting={isExisting}
          onToggle={onToggle}
        />
        <AnimatePresence>
          {expanded && (
            <DraftCardEditor
              draft={draft}
              proxies={proxies}
              index={index}
              total={total}
              errors={errors}
              isExisting={isExisting}
              hasExistingKey={hasExistingKey}
              nameRef={nameRef}
              showDeleteConfirm={showDeleteConfirm}
              onUpdate={onUpdate}
              onRemove={onRemove}
              onMove={onMove}
              onDeleteConfirm={onDeleteConfirm}
            />
          )}
        </AnimatePresence>
      </motion.div>
    );
  },
);

function DraftCardSummary({
  draft,
  index,
  expanded,
  hasErrors,
  isExisting,
  onToggle,
}: {
  draft: Draft;
  index: number;
  expanded: boolean;
  hasErrors: boolean;
  isExisting: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      className="flex w-full items-center gap-3 px-5 py-4 text-left transition-colors hover:bg-[var(--bg-3)]"
    >
      <span className="shrink-0 text-[var(--fg-2)]">
        <GripVertical className="h-3.5 w-3.5" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="shrink-0 font-mono text-xs tabular-nums text-[var(--fg-2)]">
            #{index + 1}
          </span>
          <span className="truncate text-sm font-medium text-[var(--fg-0)]">
            {draft.name || "(未命名)"}
          </span>
          {!draft.enabled && (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--fg-2)]/10 px-1.5 py-0.5 text-[10px] text-[var(--fg-2)]">
              <PowerOff className="h-2.5 w-2.5" /> 禁用
            </span>
          )}
          {hasErrors && (
            <span className="inline-flex shrink-0 items-center gap-1 rounded-[var(--radius-control)] border border-danger-border bg-danger-soft px-1.5 py-0.5 text-[10px] text-danger">
              <AlertCircle className="h-2.5 w-2.5" />
            </span>
          )}
          {!isExisting && draft.name.trim() !== "" && (
            <span className="inline-flex shrink-0 items-center rounded-[var(--radius-control)] border border-info-border bg-info-soft px-1.5 py-0.5 text-[10px] text-info">
              新增
            </span>
          )}
        </div>
        {draft.base_url && (
          <code className="mt-0.5 block truncate text-xs text-[var(--fg-2)]">
            {draft.base_url}
          </code>
        )}
        <div className="mt-1 text-[11px] text-[var(--fg-2)]">
          代理：{draft.proxy || "直连"} · 异步生图：
          {draft.image_jobs_enabled ? "支持" : "不支持"} · 用途：
          {normalizePurposes(draft.purposes).map(purposeLabel).join(" / ")}
        </div>
      </div>
      <div className="shrink-0 text-[var(--fg-2)]">
        {expanded ? (
          <ChevronUp className="h-4 w-4" />
        ) : (
          <ChevronDown className="h-4 w-4" />
        )}
      </div>
    </button>
  );
}

function DraftCardEditor({
  draft,
  proxies,
  index,
  total,
  errors,
  isExisting,
  hasExistingKey,
  nameRef,
  showDeleteConfirm,
  onUpdate,
  onRemove,
  onMove,
  onDeleteConfirm,
}: {
  draft: Draft;
  proxies: ProviderProxyOut[];
  index: number;
  total: number;
  errors?: FieldErrors;
  isExisting: boolean;
  hasExistingKey: boolean;
  nameRef: RefObject<HTMLInputElement | null>;
  showDeleteConfirm: boolean;
  onUpdate: (patch: Partial<Draft>) => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
  onDeleteConfirm: (show: boolean) => void;
}) {
  return (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: "auto", opacity: 1 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ duration: 0.2 }}
      className="overflow-hidden"
    >
      <div className="space-y-4 border-t border-[var(--border-subtle)] px-5 pb-5 pt-4">
        <DraftIdentityFields
          draft={draft}
          proxies={proxies}
          errors={errors}
          isExisting={isExisting}
          hasExistingKey={hasExistingKey}
          nameRef={nameRef}
          onUpdate={onUpdate}
        />
        <DraftExecutionFields draft={draft} onUpdate={onUpdate} />
        <DraftImageJobFields draft={draft} onUpdate={onUpdate} />
        <DraftCardActions
          index={index}
          total={total}
          showDeleteConfirm={showDeleteConfirm}
          onRemove={onRemove}
          onMove={onMove}
          onDeleteConfirm={onDeleteConfirm}
        />
      </div>
    </motion.div>
  );
}

function DraftIdentityFields({
  draft,
  proxies,
  errors,
  isExisting,
  hasExistingKey,
  nameRef,
  onUpdate,
}: {
  draft: Draft;
  proxies: ProviderProxyOut[];
  errors?: FieldErrors;
  isExisting: boolean;
  hasExistingKey: boolean;
  nameRef: RefObject<HTMLInputElement | null>;
  onUpdate: (patch: Partial<Draft>) => void;
}) {
  return (
    <>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Field label="名称" required error={errors?.name} hint="唯一标识">
          <input
            ref={nameRef}
            type="text"
            value={draft.name}
            onChange={(event) => onUpdate({ name: event.target.value })}
            placeholder="例如：主供应商"
            className={fieldCls(Boolean(errors?.name))}
          />
        </Field>
        <Field
          label="基础地址"
          required
          error={errors?.base_url}
          hint="支持 HTTP/HTTPS，可填内网地址"
        >
          <input
            type="url"
            value={draft.base_url}
            onChange={(event) => onUpdate({ base_url: event.target.value })}
            placeholder="http://10.0.0.8:8000/v1"
            className={fieldCls(Boolean(errors?.base_url))}
          />
        </Field>
      </div>

      <Field
        label="API 密钥"
        hint={providerApiKeyHint(isExisting, hasExistingKey)}
        required={!isExisting || !hasExistingKey}
      >
        <input
          type="password"
          value={draft.api_key}
          onChange={(event) => onUpdate({ api_key: event.target.value })}
          placeholder={providerApiKeyPlaceholder(isExisting, hasExistingKey)}
          autoComplete="new-password"
          className={fieldCls(false)}
        />
      </Field>

      <PurposeField draft={draft} onUpdate={onUpdate} />

      <Field label="代理" hint="供应商可直连或使用一个代理">
        <select
          value={draft.proxy ?? ""}
          onChange={(event) =>
            onUpdate({ proxy: event.target.value || null })
          }
          className={fieldCls(false)}
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
        </select>
      </Field>
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
    <Field label="用途" hint="先按用途过滤，再按健康度与权重选号">
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
                "inline-flex min-h-[36px] items-center gap-2 rounded-[var(--radius-panel)] border px-3 text-xs transition-colors disabled:cursor-not-allowed disabled:opacity-50 " +
                (checked
                  ? "border-[var(--color-lumen-amber)]/35 bg-[var(--color-lumen-amber)]/10 text-[var(--color-lumen-amber)]"
                  : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)] hover:text-[var(--fg-1)]")
              }
            >
              <span
                className={
                  "flex h-3.5 w-3.5 items-center justify-center rounded border " +
                  (checked
                    ? "border-[var(--color-lumen-amber)] bg-[var(--color-lumen-amber)] text-black"
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
    </Field>
  );
}

function DraftExecutionFields({
  draft,
  onUpdate,
}: {
  draft: Draft;
  onUpdate: (patch: Partial<Draft>) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-5">
      <Field label="优先级" hint="越大越优先">
        <input
          type="number"
          value={draft.priority}
          onChange={(event) =>
            onUpdate({
              priority: parseInt(event.target.value, 10) || 0,
            })
          }
          inputMode="numeric"
          className={fieldCls(false)}
        />
      </Field>
      <Field label="权重" hint="轮询比例">
        <input
          type="number"
          min={1}
          value={draft.weight}
          onChange={(event) =>
            onUpdate({
              weight: Math.max(1, parseInt(event.target.value, 10) || 1),
            })
          }
          inputMode="numeric"
          className={fieldCls(false)}
        />
      </Field>
      <Field label="并发数" hint="该供应商同时跑的任务上限">
        <input
          type="number"
          min={1}
          max={32}
          value={draft.image_concurrency ?? 1}
          onChange={(event) =>
            onUpdate({
              image_concurrency: Math.max(
                1,
                Math.min(32, parseInt(event.target.value, 10) || 1),
              ),
            })
          }
          inputMode="numeric"
          className={fieldCls(false)}
        />
      </Field>
      <DraftToggleField
        label="状态"
        enabled={draft.enabled}
        onClick={() => onUpdate({ enabled: !draft.enabled })}
        enabledLabel="已启用"
        disabledLabel="已禁用"
        enabledIcon={<Power className="h-3 w-3" />}
        disabledIcon={<PowerOff className="h-3 w-3" />}
      />
      <DraftToggleField
        label="异步生图"
        enabled={Boolean(draft.image_jobs_enabled)}
        onClick={() =>
          onUpdate({ image_jobs_enabled: !draft.image_jobs_enabled })
        }
        enabledLabel="支持"
        disabledLabel="不支持"
        enabledIcon={<ImageIcon className="h-3 w-3" />}
        disabledIcon={<ImageIcon className="h-3 w-3" />}
        hint="勾选后，图片任务路由才会使用这个供应商。"
        infoTone
      />
    </div>
  );
}

function DraftToggleField({
  label,
  enabled,
  onClick,
  enabledLabel,
  disabledLabel,
  enabledIcon,
  disabledIcon,
  hint,
  infoTone = false,
}: {
  label: string;
  enabled: boolean;
  onClick: () => void;
  enabledLabel: string;
  disabledLabel: string;
  enabledIcon: ReactNode;
  disabledIcon: ReactNode;
  hint?: string;
  infoTone?: boolean;
}) {
  return (
    <div className="flex flex-col">
      <span className="mb-1.5 text-xs font-medium text-[var(--fg-1)]">
        {label}
      </span>
      <button
        type="button"
        onClick={onClick}
        className={
          "inline-flex min-h-[44px] flex-1 items-center justify-center gap-1.5 rounded-[var(--radius-control)] border px-3 text-xs transition-colors sm:h-9 " +
          (enabled
            ? infoTone
              ? "border-info-border bg-info-soft text-info hover:bg-info/20"
              : "border-success-border bg-success-soft text-success hover:bg-success/20"
            : "border-[var(--border-strong)] bg-[var(--bg-3)] text-[var(--fg-2)] hover:bg-[var(--bg-3)]")
        }
      >
        {enabled ? enabledIcon : disabledIcon}
        {enabled ? enabledLabel : disabledLabel}
      </button>
      {hint && (
        <span className="mt-1 text-[11px] leading-4 text-[var(--fg-2)]">
          {hint}
        </span>
      )}
    </div>
  );
}

function DraftImageJobFields({
  draft,
  onUpdate,
}: {
  draft: Draft;
  onUpdate: (patch: Partial<Draft>) => void;
}) {
  const endpoint = draft.image_jobs_endpoint ?? "auto";
  const endpointSelected = endpoint !== "auto";
  return (
    <div className="grid grid-cols-1 gap-4 rounded-[var(--radius-panel)] border border-[var(--border-subtle)] bg-[var(--bg-2)] p-3 md:grid-cols-2">
      <div className="flex flex-col">
        <label className="mb-1.5 text-xs font-medium text-[var(--fg-1)]">
          接口偏好
        </label>
        <select
          value={endpoint}
          onChange={(event) =>
            onUpdate({
              image_jobs_endpoint:
                (event.target.value as "auto" | "generations" | "responses") ||
                "auto",
            })
          }
          className="min-h-[44px] rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-xs text-[var(--fg-1)] focus:border-info-border focus:outline-none sm:h-9"
        >
          <option value="auto">自动（按健康度自适应）</option>
          <option value="generations">
            生成接口（/v1/images/generations · /v1/images/edits）
          </option>
          <option value="responses">
            响应接口（/v1/responses + image_generation）
          </option>
        </select>
        <span className="mt-1 text-[11px] leading-4 text-[var(--fg-2)]">
          适用于异步与同步生图：自动时按健康度在两种接口间切换；锁定后该号只服务对应接口，由其他号兜底对端。
        </span>
        {endpointSelected && (
          <EndpointLockField draft={draft} onUpdate={onUpdate} />
        )}
      </div>
      {draft.image_jobs_enabled && (
        <ProviderJobOverrides draft={draft} onUpdate={onUpdate} />
      )}
    </div>
  );
}

function EndpointLockField({
  draft,
  onUpdate,
}: {
  draft: Draft;
  onUpdate: (patch: Partial<Draft>) => void;
}) {
  return (
    <>
      <button
        type="button"
        onClick={() =>
          onUpdate({
            image_jobs_endpoint_lock: !draft.image_jobs_endpoint_lock,
          })
        }
        className={
          "mt-2 inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-[var(--radius-control)] border px-3 text-xs transition-colors sm:h-8 " +
          (draft.image_jobs_endpoint_lock
            ? "border-warning-border bg-warning-soft text-warning hover:bg-warning/20"
            : "border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-2)] hover:bg-[var(--bg-3)]")
        }
      >
        {draft.image_jobs_endpoint_lock
          ? `已锁定 · 仅服务 ${endpointDisplayLabel(
              draft.image_jobs_endpoint,
            )}`
          : "锁定到该接口"}
      </button>
      <span className="mt-1 text-[11px] leading-4 text-[var(--fg-2)]">
        锁定后该号不再服务另一个接口：选号阶段直接被过滤，失败也不再回退到对端，由其它号兜底。
      </span>
    </>
  );
}

function ProviderJobOverrides({
  draft,
  onUpdate,
}: {
  draft: Draft;
  onUpdate: (patch: Partial<Draft>) => void;
}) {
  return (
    <>
      <div className="flex flex-col">
        <label className="mb-1.5 text-xs font-medium text-[var(--fg-1)]">
          旁路服务地址（可选）
        </label>
        <input
          type="url"
          placeholder="留空 = 使用全局任务旁路地址"
          value={draft.image_jobs_base_url ?? ""}
          onChange={(event) =>
            onUpdate({ image_jobs_base_url: event.target.value })
          }
          className="min-h-[44px] rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-xs text-[var(--fg-1)] placeholder:text-[var(--fg-3)] focus:border-info-border focus:outline-none sm:h-9"
        />
        <span className="mt-1 text-[11px] leading-4 text-[var(--fg-2)]">
          支持给不同供应商指定独立的图片任务旁路服务，例如多区域部署时按供应商路由。
        </span>
      </div>
      <div className="flex flex-col">
        <label className="mb-1.5 text-xs font-medium text-[var(--fg-1)]">
          编辑接口输入
        </label>
        <select
          value={draft.image_edit_input_transport ?? "url"}
          onChange={(event) =>
            onUpdate({
              image_edit_input_transport:
                (event.target.value as "url" | "file") || "url",
            })
          }
          className="min-h-[44px] rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 text-xs text-[var(--fg-1)] focus:border-info-border focus:outline-none sm:h-9"
        >
          <option value="url">链接（JSON image_url）</option>
          <option value="file">文件（multipart image[]）</option>
        </select>
        <span className="mt-1 text-[11px] leading-4 text-[var(--fg-2)]">
          只影响图片任务转发 /v1/images/edits；未启用图片任务时直连始终是 multipart 文件。
        </span>
      </div>
    </>
  );
}

function DraftCardActions({
  index,
  total,
  showDeleteConfirm,
  onRemove,
  onMove,
  onDeleteConfirm,
}: {
  index: number;
  total: number;
  showDeleteConfirm: boolean;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
  onDeleteConfirm: (show: boolean) => void;
}) {
  return (
    <div className="flex items-center gap-2 border-t border-[var(--border-subtle)] pt-3">
      <button
        type="button"
        onClick={() => onMove(-1)}
        disabled={index === 0}
        className="inline-flex min-h-[36px] items-center gap-1 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-2)] px-2 text-xs text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] disabled:opacity-25 sm:h-7"
      >
        <ChevronUp className="h-3 w-3" /> 上移
      </button>
      <button
        type="button"
        onClick={() => onMove(1)}
        disabled={index === total - 1}
        className="inline-flex min-h-[36px] items-center gap-1 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-2)] px-2 text-xs text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] disabled:opacity-25 sm:h-7"
      >
        <ChevronDown className="h-3 w-3" /> 下移
      </button>
      <div className="flex-1" />
      {showDeleteConfirm ? (
        <motion.div
          initial={{ opacity: 0, scale: 0.96 }}
          animate={{ opacity: 1, scale: 1 }}
          className="inline-flex items-center gap-2"
        >
          <span className="type-caption text-[var(--fg-2)]">确认移除?</span>
          <Button variant="danger" size="sm" onClick={onRemove}>
            移除
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => onDeleteConfirm(false)}
          >
            {copy.action.cancel}
          </Button>
        </motion.div>
      ) : (
        <Button
          variant="outline"
          size="sm"
          onClick={() => onDeleteConfirm(true)}
          leftIcon={<Trash2 className="h-3 w-3" />}
          className="border-danger-border bg-danger-soft text-danger hover:bg-danger/20"
        >
          移除
        </Button>
      )}
    </div>
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

function fieldCls(hasError: boolean): string {
  const base =
    "w-full min-h-[44px] sm:h-9 px-3 rounded-[var(--radius-control)] bg-[var(--bg-0)]/70 border text-sm font-mono text-[var(--fg-0)] focus:outline-none focus:ring-2 placeholder:text-[var(--fg-2)] transition-colors";
  if (hasError) {
    return `${base} border-danger-border focus:border-danger-border focus:ring-danger/25`;
  }
  return `${base} border-[var(--border)] focus:border-[var(--color-lumen-amber)]/50 focus:ring-[var(--color-lumen-amber)]/25`;
}

function Field({
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
          {required && <span className="ml-0.5 text-danger">*</span>}
        </span>
        {hint && !error && (
          <span className="text-[10px] text-[var(--fg-3)]">{hint}</span>
        )}
        {error && (
          <span className="flex items-center gap-0.5 text-[10px] text-danger">
            <AlertCircle className="h-2.5 w-2.5" /> {error}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}
