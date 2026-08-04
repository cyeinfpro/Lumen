"use client";

import { Save, Settings2, X } from "lucide-react";
import type { Dispatch, SetStateAction } from "react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { IconButton } from "@/components/ui/primitives/IconButton";
import { Switch } from "@/components/ui/primitives/Switch";
import type { StoryboardRun } from "@/lib/apiClient";
import { usePatchStoryboardMutation } from "@/lib/queries";
import { BottomSheet } from "@/components/ui/primitives/mobile/BottomSheet";
import { toast } from "@/components/ui/primitives/Toast";

import {
  LabeledInput,
  notifyStoryboardError,
  parseStoryboardSeed,
} from "./StoryboardShared";

interface StoryboardSettingsDraft {
  model: string;
  resolution: string;
  aspectRatio: string;
  generateAudio: boolean;
  seed: string;
}

function settingsDraftFromRun(run: StoryboardRun): StoryboardSettingsDraft {
  return {
    model: run.model,
    resolution: run.resolution,
    aspectRatio: run.aspect_ratio,
    generateAudio: run.generate_audio,
    seed: run.seed == null ? "" : String(run.seed),
  };
}

export function SettingsPanel({
  run,
  mobileOpen,
  onMobileClose,
}: {
  run: StoryboardRun;
  mobileOpen: boolean;
  onMobileClose: () => void;
}) {
  const [draft, setDraft] = useState(() => settingsDraftFromRun(run));
  const dirty =
    draft.model !== run.model ||
    draft.resolution !== run.resolution ||
    draft.aspectRatio !== run.aspect_ratio ||
    draft.generateAudio !== run.generate_audio ||
    draft.seed !== (run.seed == null ? "" : String(run.seed));
  const patch = usePatchStoryboardMutation(run.id, {
    onSuccess: (data) => {
      setDraft(settingsDraftFromRun(data));
      toast.success("视频参数已保存");
    },
    onError: notifyStoryboardError("保存视频参数"),
  });
  const parsedSeed = parseStoryboardSeed(draft.seed);
  const seedInvalid = Boolean(draft.seed.trim()) && parsedSeed === null;
  const saveBlocked =
    patch.isPending ||
    seedInvalid ||
    !dirty ||
    !draft.model.trim() ||
    !draft.resolution.trim() ||
    !draft.aspectRatio.trim();
  const save = () =>
    patch.mutate({
      model: draft.model.trim(),
      resolution: draft.resolution.trim(),
      aspect_ratio: draft.aspectRatio.trim(),
      generate_audio: draft.generateAudio,
      seed: parsedSeed,
    });
  const fields = (
    <StoryboardSettingsFields
      draft={draft}
      dirty={dirty}
      seedInvalid={seedInvalid}
      saving={patch.isPending}
      saveBlocked={saveBlocked}
      onChange={setDraft}
      onReset={() => setDraft(settingsDraftFromRun(run))}
      onSave={save}
    />
  );

  return (
    <>
      <aside className="hidden min-h-0 overflow-y-auto p-3 lg:block">
        {fields}
      </aside>
      <BottomSheet
        open={mobileOpen}
        onClose={onMobileClose}
        ariaLabel="视频参数"
        snapPoints={["88%"]}
      >
        <header className="flex shrink-0 items-center justify-between border-b border-[var(--border)] px-5 py-3">
          <div className="flex items-center gap-2">
            <Settings2 className="h-4 w-4 text-[var(--accent)]" />
            <h2 className="type-card-title">视频参数</h2>
          </div>
          <IconButton
            variant="ghost"
            size="sm"
            onClick={onMobileClose}
            aria-label="关闭视频参数"
          >
            <X className="h-4 w-4" />
          </IconButton>
        </header>
        <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto px-4 pb-[var(--mobile-dialog-footer-pad-bottom)] pt-3">
          {fields}
        </div>
      </BottomSheet>
    </>
  );
}

function StoryboardSettingsFields({
  draft,
  dirty,
  seedInvalid,
  saving,
  saveBlocked,
  onChange,
  onReset,
  onSave,
}: {
  draft: StoryboardSettingsDraft;
  dirty: boolean;
  seedInvalid: boolean;
  saving: boolean;
  saveBlocked: boolean;
  onChange: Dispatch<SetStateAction<StoryboardSettingsDraft>>;
  onReset: () => void;
  onSave: () => void;
}) {
  return (
    <div className="surface-card grid gap-3 p-3">
      <div className="hidden items-center gap-2 lg:flex">
        <Settings2 className="h-4 w-4 text-[var(--accent)]" />
        <h2 className="type-card-title">视频参数</h2>
      </div>
      <LabeledInput
        label="模型"
        value={draft.model}
        onChange={(model) => onChange((current) => ({ ...current, model }))}
      />
      <LabeledInput
        label="分辨率"
        value={draft.resolution}
        onChange={(resolution) =>
          onChange((current) => ({ ...current, resolution }))
        }
      />
      <LabeledInput
        label="比例"
        value={draft.aspectRatio}
        onChange={(aspectRatio) =>
          onChange((current) => ({ ...current, aspectRatio }))
        }
      />
      <LabeledInput
        label="Seed"
        value={draft.seed}
        onChange={(seed) => onChange((current) => ({ ...current, seed }))}
      />
      {seedInvalid ? (
        <p className="type-caption text-[var(--danger)]" role="alert">
          Seed 需为 -1 到 4294967295 的整数
        </p>
      ) : null}
      <div className="control-shell flex min-h-11 items-center justify-between gap-3 px-3">
        <span className="type-body-sm text-[var(--fg-1)]">生成音频</span>
        <Switch
          aria-label="生成音频"
          checked={draft.generateAudio}
          onCheckedChange={(checked) =>
            onChange((current) => ({
              ...current,
              generateAudio: checked,
            }))
          }
        />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Button
          variant="outline"
          onClick={onReset}
          disabled={!dirty || saving}
          fullWidth
        >
          取消修改
        </Button>
        <Button
          variant="primary"
          disabled={saveBlocked}
          onClick={onSave}
          loading={saving}
          leftIcon={<Save className="h-4 w-4" />}
          fullWidth
        >
          保存参数
        </Button>
      </div>
    </div>
  );
}
