"use client";

import { Upload } from "lucide-react";
import { useRef, useState } from "react";

import { Button, Input } from "@/components/ui/primitives";
import { canvasFixedSizeError } from "@/lib/canvas/graph";

type SelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
};

export function FixedSizeInput({
  value,
  onCommit,
}: {
  value: string;
  onCommit: (value: string) => void;
}) {
  return (
    <FixedSizeInputControl
      key={value}
      value={value}
      onCommit={onCommit}
    />
  );
}

function FixedSizeInputControl({
  value,
  onCommit,
}: {
  value: string;
  onCommit: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  const normalized = draft.trim().toLowerCase().replace(/\s+/g, "");
  const error = canvasFixedSizeError(normalized);
  const valid = error === null;
  return (
    <Input
      label="固定尺寸"
      value={draft}
      inputMode="text"
      placeholder="1536x1024"
      maxLength={32}
      invalid={!valid}
      error={error ?? undefined}
      onChange={(event) => setDraft(event.currentTarget.value)}
      onBlur={() => {
        if (valid && normalized !== value) onCommit(normalized);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" && valid) {
          event.preventDefault();
          event.currentTarget.blur();
        }
        if (event.key === "Escape") {
          event.preventDefault();
          setDraft(value);
          event.currentTarget.blur();
        }
      }}
    />
  );
}

export function OptionalSeedInput({
  value,
  onCommit,
}: {
  value: number | null;
  onCommit: (value: number | null) => void;
}) {
  return (
    <OptionalSeedInputControl
      key={value === null ? "random" : value}
      value={value}
      onCommit={onCommit}
    />
  );
}

function OptionalSeedInputControl({
  value,
  onCommit,
}: {
  value: number | null;
  onCommit: (value: number | null) => void;
}) {
  const external = value === null ? "" : String(value);
  const [draft, setDraft] = useState(external);
  const parsed = draft.trim() === "" ? null : Number(draft);
  const valid =
    parsed === null ||
    (Number.isInteger(parsed) && parsed >= -1 && parsed <= 4_294_967_295);
  return (
    <Input
      label="种子"
      type="number"
      inputMode="numeric"
      min={-1}
      max={4_294_967_295}
      step={1}
      value={draft}
      placeholder="留空为随机"
      invalid={!valid}
      error={valid ? undefined : "请输入 -1 至 4294967295 的整数"}
      onChange={(event) => setDraft(event.currentTarget.value)}
      onBlur={() => {
        if (valid && draft !== external) onCommit(parsed);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" && valid) {
          event.preventDefault();
          event.currentTarget.blur();
        }
        if (event.key === "Escape") {
          event.preventDefault();
          setDraft(external);
          event.currentTarget.blur();
        }
      }}
    />
  );
}

export function UploadField({
  accept,
  busy,
  label,
  onSelect,
}: {
  accept: string;
  busy: boolean;
  label: string;
  onSelect: (file: File) => Promise<void>;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <>
      <Button
        type="button"
        variant="outline"
        fullWidth
        loading={busy}
        leftIcon={busy ? undefined : <Upload className="h-4 w-4" />}
        onClick={() => inputRef.current?.click()}
      >
        {busy ? "上传中" : label}
      </Button>
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        className="sr-only"
        disabled={busy}
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = "";
          if (file) void onSelect(file);
        }}
      />
    </>
  );
}

export function ReadOnlyValue({
  label,
  value,
}: {
  label: string;
  value: string;
}) {
  return (
    <div className="flex min-h-9 items-center justify-between gap-3 type-body-sm">
      <span className="text-[var(--fg-2)]">{label}</span>
      <span className="truncate text-[var(--fg-0)]">{value}</span>
    </div>
  );
}

export function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)] p-3">
      <p className="type-caption text-[var(--fg-2)]">{label}</p>
      <p className="mt-1 truncate type-body-sm font-semibold tabular-nums text-[var(--fg-0)]">
        {value}
      </p>
    </div>
  );
}

export function normalizedCrop(value: unknown): {
  x: number;
  y: number;
  width: number;
  height: number;
} | null {
  if (!value || typeof value !== "object") return null;
  const crop = value as Record<string, unknown>;
  const x = Number(crop.x);
  const y = Number(crop.y);
  const width = Number(crop.width);
  const height = Number(crop.height);
  if (
    ![x, y, width, height].every(Number.isFinite) ||
    x < 0 ||
    y < 0 ||
    width <= 0 ||
    height <= 0 ||
    x + width > 1 ||
    y + height > 1
  ) {
    return null;
  }
  return { x, y, width, height };
}

function selectOptions(
  values: string[],
  known: readonly SelectOption[],
): SelectOption[] {
  const labels = new Map(known.map((item) => [item.value, item.label]));
  return values.map((value) => ({
    value,
    label: labels.get(value) ?? value.toUpperCase(),
  }));
}

export function selectOptionsWithCurrent(
  values: string[],
  current: string,
  known: readonly SelectOption[],
  optionsLoaded: boolean,
): SelectOption[] {
  const options: SelectOption[] = selectOptions(values, known);
  if (!optionsLoaded || !current || values.includes(current)) return options;
  const label =
    known.find((item) => item.value === current)?.label ?? current.toUpperCase();
  return [
    {
      value: current,
      label: `${label}（当前不可用）`,
      disabled: true,
    },
    ...options,
  ];
}

export function uniqueStrings(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean)));
}

export function videoModeLabel(mode: string): string {
  return (
    {
      t2v: "文生视频",
      i2v: "首帧生视频",
      reference: "参考媒体生成",
    }[mode] ?? mode
  );
}
