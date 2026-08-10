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
  Trash2,
} from "lucide-react";
import type { ProviderProxyOut } from "@/lib/types";
import {
  Button,
  Input,
  Select,
  StatusBadge,
  Switch,
} from "@/components/ui/primitives";
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
          "surface-card overflow-hidden transition-colors " +
          (expanded
            ? hasErrors
              ? "border-danger-border bg-danger-soft"
              : "border-[var(--accent)]/45 bg-[var(--accent)]/[0.04]"
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
          <span className="shrink-0 font-mono type-caption tabular-nums text-[var(--fg-2)]">
            #{index + 1}
          </span>
          <span className="truncate type-body-sm font-medium text-[var(--fg-0)]">
            {draft.name || "(未命名)"}
          </span>
          {!draft.enabled && (
            <StatusBadge status="disabled" />
          )}
          {hasErrors && (
            <StatusBadge status="error" label={<AlertCircle className="h-3 w-3" />} />
          )}
          {!isExisting && draft.name.trim() !== "" && (
            <StatusBadge status="info" label="新增" />
          )}
        </div>
        {draft.base_url && (
          <code className="mt-0.5 block truncate type-caption text-[var(--fg-2)]">
            {draft.base_url}
          </code>
        )}
        <div className="mt-1 type-caption text-[var(--fg-2)]">
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
        <Input
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
          <Input
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
        <Input
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
        <Select
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
        </Select>
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
    <div className="grid grid-cols-1 gap-4 md:grid-cols-3 xl:grid-cols-6">
      <Field label="优先级" hint="越大越优先">
          <Input
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
        <Input
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
        <Input
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
        onCheckedChange={(enabled) => onUpdate({ enabled })}
        enabledLabel="已启用"
        disabledLabel="已禁用"
      />
      <DraftToggleField
        label="异步生图"
        enabled={Boolean(draft.image_jobs_enabled)}
        onCheckedChange={(enabled) => onUpdate({ image_jobs_enabled: enabled })}
        enabledLabel="支持"
        disabledLabel="不支持"
        hint="勾选后，图片任务路由才会使用这个供应商。"
        infoTone
      />
      <DraftToggleField
        label="流式生图"
        enabled={Boolean(draft.image_streaming_enabled)}
        onCheckedChange={(enabled) =>
          onUpdate({ image_streaming_enabled: enabled })
        }
        enabledLabel="已开启"
        disabledLabel="已关闭"
        hint="支持 Images API stream，最终图片事件到达后立即结束等待。"
        infoTone
      />
    </div>
  );
}

function DraftToggleField({
  label,
  enabled,
  onCheckedChange,
  enabledLabel,
  disabledLabel,
  hint,
  infoTone = false,
}: {
  label: string;
  enabled: boolean;
  onCheckedChange: (enabled: boolean) => void;
  enabledLabel: string;
  disabledLabel: string;
  hint?: string;
  infoTone?: boolean;
}) {
  return (
    <div className="flex flex-col">
      <span className="mb-1.5 type-caption font-medium text-[var(--fg-1)]">
        {label}
      </span>
      <div
        className={`flex min-h-10 flex-1 items-center justify-between gap-2 rounded-[var(--radius-control)] border px-3 ${
          enabled
            ? infoTone
              ? "border-info-border bg-info-soft"
              : "border-success-border bg-success-soft"
            : "border-[var(--border-strong)] bg-[var(--bg-3)]"
        }`}
      >
        <span className="type-caption text-[var(--fg-1)]">
          {enabled ? enabledLabel : disabledLabel}
        </span>
        <Switch
          checked={enabled}
          onCheckedChange={onCheckedChange}
          aria-label={`${label}${enabled ? "关闭" : "开启"}`}
        />
      </div>
      {hint && (
        <span className="mt-1 type-caption leading-4 text-[var(--fg-2)]">
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
        <label className="mb-1.5 type-caption font-medium text-[var(--fg-1)]">
          接口偏好
        </label>
        <Select
          value={endpoint}
          onChange={(event) =>
            onUpdate({
              image_jobs_endpoint:
                (event.target.value as "auto" | "generations" | "responses") ||
                "auto",
            })
          }
          className="min-h-[44px] rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 type-caption text-[var(--fg-1)] focus:border-info-border focus:outline-none sm:h-9"
        >
          <option value="auto">自动（按健康度自适应）</option>
          <option value="generations">
            生成接口（/v1/images/generations · /v1/images/edits）
          </option>
          <option value="responses">
            响应接口（/v1/responses + image_generation）
          </option>
        </Select>
        <span className="mt-1 type-caption leading-4 text-[var(--fg-2)]">
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
      <div className="mt-2 flex items-center justify-between gap-2 rounded-[var(--radius-control)] border border-warning-border bg-warning-soft px-3 py-2">
        <span className="type-caption text-warning">
          {draft.image_jobs_endpoint_lock
            ? `已锁定 · 仅服务 ${endpointDisplayLabel(
                draft.image_jobs_endpoint,
              )}`
            : "锁定到该接口"}
        </span>
        <Switch
          checked={Boolean(draft.image_jobs_endpoint_lock)}
          onCheckedChange={(checked) =>
            onUpdate({ image_jobs_endpoint_lock: checked })
          }
          aria-label="锁定生图接口"
        />
      </div>
      <span className="mt-1 type-caption leading-4 text-[var(--fg-2)]">
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
        <label className="mb-1.5 type-caption font-medium text-[var(--fg-1)]">
          旁路服务地址（可选）
        </label>
        <Input
          type="url"
          placeholder="留空 = 使用全局任务旁路地址"
          value={draft.image_jobs_base_url ?? ""}
          onChange={(event) =>
            onUpdate({ image_jobs_base_url: event.target.value })
          }
          className="min-h-[44px] rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 type-caption text-[var(--fg-1)] placeholder:text-[var(--fg-3)] focus:border-info-border focus:outline-none sm:h-9"
        />
        <span className="mt-1 type-caption leading-4 text-[var(--fg-2)]">
          支持给不同供应商指定独立的图片任务旁路服务，例如多区域部署时按供应商路由。
        </span>
      </div>
      <div className="flex flex-col">
        <label className="mb-1.5 type-caption font-medium text-[var(--fg-1)]">
          编辑接口输入
        </label>
        <Select
          value={draft.image_edit_input_transport ?? "url"}
          onChange={(event) =>
            onUpdate({
              image_edit_input_transport:
                (event.target.value as "url" | "file") || "url",
            })
          }
          className="min-h-[44px] rounded-[var(--radius-control)] border border-[var(--border)] bg-[var(--bg-2)] px-3 type-caption text-[var(--fg-1)] focus:border-info-border focus:outline-none sm:h-9"
        >
          <option value="url">链接（JSON image_url）</option>
          <option value="file">文件（multipart image[]）</option>
        </Select>
        <span className="mt-1 type-caption leading-4 text-[var(--fg-2)]">
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
        className="inline-flex min-h-[36px] items-center gap-1 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-2)] px-2 type-caption text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] disabled:opacity-25 sm:h-7"
      >
        <ChevronUp className="h-3 w-3" /> 上移
      </button>
      <button
        type="button"
        onClick={() => onMove(1)}
        disabled={index === total - 1}
        className="inline-flex min-h-[36px] items-center gap-1 rounded-[var(--radius-card)] border border-[var(--border)] bg-[var(--bg-2)] px-2 type-caption text-[var(--fg-1)] transition-colors hover:bg-[var(--bg-3)] disabled:opacity-25 sm:h-7"
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
  const base = "font-mono";
  if (hasError) {
    return `${base} border-danger-border focus:border-danger-border focus:ring-danger/25`;
  }
  return `${base} border-[var(--border)] focus:border-accent-border focus:ring-accent/20`;
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
          <span className="type-caption text-[var(--fg-2)]">{hint}</span>
        )}
        {error && (
          <span className="flex items-center gap-0.5 type-caption text-danger">
            <AlertCircle className="h-3 w-3" /> {error}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}
