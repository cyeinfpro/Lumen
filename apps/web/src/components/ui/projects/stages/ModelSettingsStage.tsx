"use client";

// 模特设定：把 user_prompt 作为风格初值；商品约束阶段推荐的配饰进入后续配饰四宫格。
// 失败 toast；按钮 loading 时禁用。

import { Library, Sparkles } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import { useCreateModelCandidatesMutation } from "@/lib/queries";
import type { WorkflowRun } from "@/lib/apiClient";
import type { ModelLibraryAgeSegment } from "@/lib/apiClient";
import { ModelLibraryDialog } from "../components/ModelLibraryDialog";
import { StageFrame } from "../components/StageFrame";
import { accessorySuggestionText } from "../utils";

export function ModelSettingsStage({ workflow }: { workflow: WorkflowRun }) {
  const create = useCreateModelCandidatesMutation(workflow.id, {
    onError: (err) => {
      toast.error("生成模特候选失败", {
        description: err instanceof Error ? err.message : "请稍后重试",
      });
    },
    onSuccess: () => toast.success("已派发 3 套模特候选生成"),
  });
  const [stylePrompt, setStylePrompt] = useState(workflow.user_prompt);
  const [avoid, setAvoid] = useState("过度网红感、夸张姿势、强烈妆容");
  const [accessoryEnabled, setAccessoryEnabled] = useState(true);
  const suggestedAccessories = accessorySuggestionText(workflow);
  const [accessories, setAccessories] = useState(
    suggestedAccessories || "简洁鞋子、小巧发饰、轻量包袋",
  );
  const [libraryOpen, setLibraryOpen] = useState(false);

  const submit = () => {
    if (!stylePrompt.trim()) {
      toast.warning("请填写风格方向");
      return;
    }
    create.mutate({
      candidate_count: 3,
      style_prompt: stylePrompt,
      avoid: avoid
        .split(/[、,]/)
        .map((item) => item.trim())
        .filter(Boolean),
      accessory_plan: {
        enabled: accessoryEnabled,
        items: accessoryEnabled
          ? accessories
              .split(/[,，、]/)
              .map((item) => item.trim())
              .filter(Boolean)
          : [],
        strength: "subtle",
      },
    });
  };
  const defaultAgeSegment = defaultLibraryAgeSegment(workflow);

  return (
    <StageFrame
      title="模特设定"
      subtitle="先确认模特本人。配饰会在确认模特后生成四宫格参考，不会提前试穿商品。"
    >
      <label className="block">
        <span className="text-sm text-[var(--fg-1)]">风格方向</span>
        <textarea
          value={stylePrompt}
          onChange={(event) => setStylePrompt(event.target.value)}
          rows={4}
          className="mt-2 w-full resize-none rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 py-2 text-sm leading-6 outline-none transition-colors focus:border-[var(--border-amber)]"
          placeholder="高级通勤感，冷淡气质模特，适合独立站女装"
        />
      </label>
      <label className="mt-4 block">
        <span className="text-sm text-[var(--fg-1)]">禁用项</span>
        <input
          value={avoid}
          onChange={(event) => setAvoid(event.target.value)}
          className="mt-2 h-10 w-full rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm outline-none transition-colors focus:border-[var(--border-amber)]"
          placeholder="顿号或逗号分隔，例如 网红感、夸张姿势"
        />
      </label>
      <label className="mt-4 block">
        <span className="text-sm text-[var(--fg-1)]">配饰四宫格方向</span>
        <div className="mt-2 flex gap-2">
          <button
            type="button"
            onClick={() => setAccessoryEnabled((value) => !value)}
            className={[
              "h-10 rounded-md border px-3 text-sm transition-colors",
              accessoryEnabled
                ? "border-[var(--border-amber)] bg-[var(--accent-soft)] text-[var(--amber-300)]"
                : "border-[var(--border)] bg-[var(--bg-1)] text-[var(--fg-1)]",
            ].join(" ")}
          >
            {accessoryEnabled ? "开启" : "关闭"}
          </button>
          <input
            value={accessories}
            onChange={(event) => setAccessories(event.target.value)}
            disabled={!accessoryEnabled}
            className="h-10 min-w-0 flex-1 rounded-md border border-[var(--border)] bg-[var(--bg-1)] px-3 text-sm outline-none transition-colors focus:border-[var(--border-amber)] disabled:opacity-50"
            placeholder="逗号或顿号分隔，例如 白色运动鞋、小发夹"
          />
        </div>
      </label>
      <div className="mt-4 rounded-md border border-[var(--border-amber)]/40 bg-[var(--accent-soft)] p-3 text-sm leading-6 text-[var(--fg-1)]">
        模特方案图未试穿商品，仅用于确认模特形象。确认模特后，会基于该模特生成带配饰的白底四宫格参考图，最终展示图会参考你选中的配饰方案。
      </div>
      <div className="mt-4 flex flex-col gap-2 sm:flex-row">
        <Button
          variant="primary"
          onClick={() => setLibraryOpen(true)}
          leftIcon={<Library className="h-4 w-4" />}
        >
          打开模特库
        </Button>
        <Button
          variant="secondary"
          loading={create.isPending}
          onClick={submit}
          leftIcon={<Sparkles className="h-4 w-4" />}
        >
          生成模特候选
        </Button>
      </div>
      <ModelLibraryDialog
        key={`${workflow.id}:${defaultAgeSegment}`}
        open={libraryOpen}
        workflow={workflow}
        defaultAgeSegment={defaultAgeSegment}
        onClose={() => setLibraryOpen(false)}
        generatingCandidates={create.isPending}
        onGenerateCandidates={() => {
          setLibraryOpen(false);
          submit();
        }}
      />
    </StageFrame>
  );
}

function defaultLibraryAgeSegment(workflow: WorkflowRun): ModelLibraryAgeSegment {
  const profile = workflow.metadata_jsonb?.model_profile;
  if (profile && typeof profile === "object" && "age_segment" in profile) {
    const value = (profile as { age_segment?: unknown }).age_segment;
    if (typeof value === "string" && isLibraryAgeSegment(value)) return value;
  }
  const text = workflow.user_prompt;
  if (text.includes("幼儿")) return "toddler";
  if (["儿童", "童装", "小朋友", "孩子"].some((word) => text.includes(word))) return "child";
  if (text.includes("青少年")) return "teen";
  if (text.includes("青年")) return "young_adult";
  if (text.includes("中老年")) return "middle_aged";
  if (text.includes("老年")) return "senior";
  if (text.includes("成年")) return "adult";
  return "all";
}

function isLibraryAgeSegment(value: string): value is ModelLibraryAgeSegment {
  return [
    "all",
    "user_favorites",
    "toddler",
    "child",
    "teen",
    "young_adult",
    "adult",
    "middle_aged",
    "senior",
  ].includes(value);
}
