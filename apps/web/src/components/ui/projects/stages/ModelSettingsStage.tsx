"use client";

// 模特设定（editorial 重构）：
// • 把 user_prompt 作为风格初值；商品约束阶段推荐的配饰进入后续配饰四宫格。
// • hairline 分隔取代嵌套卡；底部 underline 输入；toggle 改 mono dot 风格。
// • 失败 toast；按钮 loading 时禁用。

import { Library, Sparkles } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { toast } from "@/components/ui/primitives/Toast";
import { useCreateModelCandidatesMutation } from "@/lib/queries";
import type { WorkflowRun } from "@/lib/apiClient";
import { cn } from "@/lib/utils";
import { ModelLibraryDialog } from "../components/ModelLibraryDialog";
import { StageFrame } from "../components/StageFrame";
import { accessorySuggestionText, defaultLibraryAgeSegment } from "../utils";

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
      toast.warning("风格方向未填");
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
      eyebrow="N°03 — 模特设置"
      title="模特设定"
      subtitle="先确认模特本人。配饰会在确认模特后生成四宫格参考，不会提前试穿商品。"
    >
      <section className="border-t border-[var(--border)] py-4">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Style Direction
        </p>
        <textarea
          value={stylePrompt}
          onChange={(event) => setStylePrompt(event.target.value)}
          rows={4}
          className="mt-3 w-full resize-none border-b border-[var(--border)] bg-transparent px-1 py-2 text-[14px] leading-7 text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
          placeholder="高级通勤感，冷淡气质模特，适合独立站女装"
        />
      </section>

      <section className="border-t border-[var(--border)] py-4">
        <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Avoid
        </p>
        <input
          value={avoid}
          onChange={(event) => setAvoid(event.target.value)}
          className="mt-3 h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)]"
          placeholder="顿号或逗号分隔，例如 网红感、夸张姿势"
        />
      </section>

      <section className="border-t border-[var(--border)] py-4">
        <div className="flex items-center justify-between gap-3">
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
            Accessory Quad
          </p>
          <button
            type="button"
            onClick={() => setAccessoryEnabled((value) => !value)}
            className="inline-flex cursor-pointer items-center gap-2 font-mono text-[10px] uppercase tracking-[0.18em] transition-colors"
          >
            <span
              aria-hidden
              className={cn(
                "inline-block h-1.5 w-1.5 rounded-full transition-colors",
                accessoryEnabled ? "bg-[var(--amber-400)]" : "bg-[var(--fg-3)]",
              )}
            />
            <span
              className={
                accessoryEnabled ? "text-[var(--amber-300)]" : "text-[var(--fg-2)]"
              }
            >
              {accessoryEnabled ? "Enabled" : "Disabled"}
            </span>
          </button>
        </div>
        <input
          value={accessories}
          onChange={(event) => setAccessories(event.target.value)}
          disabled={!accessoryEnabled}
          className="mt-3 h-10 w-full border-b border-[var(--border)] bg-transparent px-1 text-[14px] text-[var(--fg-0)] outline-none transition-colors placeholder:text-[var(--fg-3)] focus:border-[var(--amber-400)] disabled:opacity-40"
          placeholder="逗号或顿号分隔，例如 白色运动鞋、小发夹"
        />
        <p className="mt-3 max-w-2xl break-words text-[12px] leading-6 text-[var(--fg-2)]">
          模特方案图未试穿商品，仅用于确认模特形象。确认模特后会基于该模特生成带配饰的白底四宫格参考图，最终展示图会参考你选中的配饰方案。
        </p>
      </section>

      <div className="grid grid-cols-1 gap-2 border-t border-[var(--border)] pt-5 min-[420px]:grid-cols-2 sm:flex sm:flex-row">
        <Button
          variant="outline"
          onClick={() => setLibraryOpen(true)}
          leftIcon={<Library className="h-4 w-4" />}
          className="w-full sm:w-auto"
        >
          打开模特库
        </Button>
        <Button
          variant="primary"
          loading={create.isPending}
          onClick={submit}
          leftIcon={<Sparkles className="h-4 w-4" />}
          className="w-full sm:w-auto"
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
        selectionAccessoryPlan={{
          enabled: accessoryEnabled,
          items: accessoryEnabled
            ? accessories
                .split(/[,，、]/)
                .map((item) => item.trim())
                .filter(Boolean)
            : [],
          strength: "subtle",
        }}
        selectionStylePrompt={stylePrompt}
        onGenerateCandidates={() => {
          setLibraryOpen(false);
          submit();
        }}
      />
    </StageFrame>
  );
}
