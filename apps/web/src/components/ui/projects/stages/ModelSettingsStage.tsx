"use client";

// 模特设定（editorial 重构）：
// • 把 user_prompt 作为风格初值；商品约束阶段推荐的配饰进入后续配饰四宫格。
// • hairline 分隔取代嵌套卡；表单统一 control-shell。
// • 失败 toast；按钮 loading 时禁用。

import { Library, Sparkles } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/primitives/Button";
import { Input } from "@/components/ui/primitives/Input";
import { Switch } from "@/components/ui/primitives/Switch";
import { Textarea } from "@/components/ui/primitives/Textarea";
import { toast } from "@/components/ui/primitives/Toast";
import { useCreateModelCandidatesMutation } from "@/lib/queries";
import type { WorkflowRun } from "@/lib/apiClient";
import { ModelLibraryDialog } from "../components/ModelLibraryDialog";
import { StageFrame } from "../components/StageFrame";
import { accessorySuggestionText, defaultLibraryAgeSegment } from "../utils";

export function ModelSettingsStage({ workflow }: { workflow: WorkflowRun }) {
  const create = useCreateModelCandidatesMutation(workflow.id, {
    onError: (err) => {
      toast.error("生成模特候选失败", {
        description: err instanceof Error ? err.message : "稍后重试",
      });
    },
    onSuccess: () => toast.success("已派发 3 套模特候选生成"),
  });
  const [stylePrompt, setStylePrompt] = useState(workflow.user_prompt);
  const [avoid, setAvoid] = useState("过度网红感、夸张姿势、强烈妆容");
  const [accessoryOn, setAccessoryOn] = useState(true);
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
        enabled: accessoryOn,
        items: accessoryOn
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
        <p className="type-label text-[var(--fg-1)]">风格方向</p>
        <Textarea
          value={stylePrompt}
          onChange={(event) => setStylePrompt(event.target.value)}
          rows={4}
          wrapperClassName="mt-3"
          className="resize-none"
          placeholder="高级通勤感，冷淡气质模特，适合独立站女装"
        />
      </section>

      <section className="border-t border-[var(--border)] py-4">
        <p className="type-label text-[var(--fg-1)]">避免特征</p>
        <Input
          value={avoid}
          onChange={(event) => setAvoid(event.target.value)}
          wrapperClassName="mt-3"
          placeholder="顿号或逗号分隔，例如 网红感、夸张姿势"
        />
      </section>

      <section className="border-t border-[var(--border)] py-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="type-label text-[var(--fg-1)]">配饰四宫格</p>
            <p className="type-caption mt-0.5 text-[var(--fg-2)]">
              {accessoryOn ? "已启用" : "已停用"}
            </p>
          </div>
          <Switch
            aria-label="配饰四宫格"
            checked={accessoryOn}
            onCheckedChange={setAccessoryOn}
          />
        </div>
        <Input
          value={accessories}
          onChange={(event) => setAccessories(event.target.value)}
          disabled={!accessoryOn}
          wrapperClassName="mt-3"
          placeholder="逗号或顿号分隔，例如 白色运动鞋、小发夹"
        />
        <p className="type-caption mt-3 max-w-2xl break-words text-[var(--fg-2)]">
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
          enabled: accessoryOn,
          items: accessoryOn
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
