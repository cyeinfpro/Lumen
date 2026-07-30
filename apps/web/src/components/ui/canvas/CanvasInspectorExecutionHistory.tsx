import { useState, type ComponentType } from "react";

import {
  canvasExecutionElapsedMs,
  canvasExecutionPrimaryTask,
  canvasExecutionProgressPercent,
  canvasExecutionStageLabel,
  canvasExecutionStatusLabel,
  formatCanvasTaskElapsed,
  isCanvasExecutionActive,
} from "@/lib/canvas/executionPresentation";
import type {
  CanvasDocument,
  CanvasExecutionTaskDetail,
  CanvasNodeExecution,
  CanvasOutput,
} from "@/lib/canvas/types";
import { useSelectCanvasOutputMutation } from "@/lib/queries/canvases";
import { cn } from "@/lib/utils";
import { toast } from "@/components/ui/primitives";
import { InspectorSection } from "./CanvasInspectorFields";

export interface CanvasHistoryOutputProps {
  output: CanvasOutput;
  index: number;
  active: boolean;
  loading: boolean;
  onSelect: () => void;
}

export function CanvasInspectorExecutionHistory({
  executions,
  document,
  selectedNodeId,
  OutputComponent,
}: {
  executions: CanvasNodeExecution[];
  document: CanvasDocument;
  selectedNodeId: string;
  OutputComponent: ComponentType<CanvasHistoryOutputProps>;
}) {
  const selectOutput = useSelectCanvasOutputMutation(document.id);
  const current = document.selections.find(
    (selection) => selection.node_id === selectedNodeId,
  );
  return (
    <InspectorSection title="历史输出">
      <div className="grid gap-2">
        {executions.map((execution) => (
          <div
            key={execution.id}
            className="border-b border-[var(--border-subtle)] pb-3 last:border-0"
          >
            <div className="flex items-center justify-between gap-2">
              <span
                className={cn(
                  "type-caption font-medium",
                  execution.status === "partial_failed"
                    ? "text-[var(--warning-fg)]"
                    : "text-[var(--fg-2)]",
                )}
              >
                {canvasExecutionStatusLabel(execution.status)}
              </span>
              <span className="type-caption text-[var(--fg-3)]">
                {execution.created_at
                  ? new Date(execution.created_at).toLocaleString("zh-CN")
                  : ""}
              </span>
            </div>
            <ExecutionTaskDetails execution={execution} />
            {execution.error_message ||
            canvasExecutionPrimaryTask(execution)?.error_message ? (
              <p
                role={execution.status === "partial_failed" ? "status" : "alert"}
                className={cn(
                  "mt-2 type-caption",
                  execution.status === "partial_failed"
                    ? "text-[var(--warning-fg)]"
                    : "text-[var(--danger-fg)]",
                )}
              >
                {execution.error_message ??
                  canvasExecutionPrimaryTask(execution)?.error_message}
              </p>
            ) : null}
            {execution.outputs.length > 0 ? (
              <div className="mt-2 grid grid-cols-3 gap-2">
                {execution.outputs.map((output, index) => (
                  <OutputComponent
                    key={`${execution.id}:${index}`}
                    output={output}
                    index={index}
                    active={
                      current?.execution_id === execution.id &&
                      current.output_index === index
                    }
                    loading={
                      selectOutput.isPending &&
                      selectOutput.variables?.nodeId === execution.node_id
                    }
                    onSelect={() =>
                      selectOutput.mutate(
                        {
                          nodeId: execution.node_id,
                          executionId: execution.id,
                          outputIndex: index,
                          selectionRevision: current?.revision,
                        },
                        {
                          onError: (error) => toast.error(error.message),
                        },
                      )
                    }
                  />
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </InspectorSection>
  );
}

function ExecutionTaskDetails({
  execution,
}: {
  execution: CanvasNodeExecution;
}) {
  const task = canvasExecutionPrimaryTask(execution);
  const active = isCanvasExecutionActive(execution);
  const [detailsOpen, setDetailsOpen] = useState(active);
  if (!task && !active) return null;
  const progress = canvasExecutionProgressPercent(execution);
  const stage = canvasExecutionStageLabel(execution);
  const elapsed = formatCanvasTaskElapsed(canvasExecutionElapsedMs(execution));
  const rows = task ? executionTaskRows(task, elapsed) : [];
  return (
    <div className="mt-2 rounded-[var(--radius-control)] border border-[var(--border-subtle)] bg-[var(--bg-0)]/56 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="type-caption font-medium text-[var(--fg-1)]">
          {stage}
        </span>
        <span className="type-mono-meta tabular-nums text-[var(--fg-2)]">
          {progress !== null
            ? `${progress}%`
            : elapsed
              ? `已用 ${elapsed}`
              : "进行中"}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={`${stage}进度`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress ?? undefined}
        className="mt-2 h-1.5 overflow-hidden rounded-full bg-[var(--bg-3)]"
      >
        <span
          className={cn(
            "block h-full rounded-full bg-[var(--accent)]",
            progress === null
              ? "w-1/3 animate-pulse motion-reduce:animate-none"
              : "w-full origin-left transition-transform duration-[var(--dur-base)] ease-[var(--ease-develop)]",
          )}
          style={
            progress === null
              ? undefined
              : { transform: `scaleX(${progress / 100})` }
          }
        />
      </div>
      {rows.length > 0 ? (
        <details
          className="mt-2"
          open={detailsOpen}
          onToggle={(event) => setDetailsOpen(event.currentTarget.open)}
        >
          <summary className="cursor-pointer type-caption text-[var(--fg-2)]">
            任务详情
          </summary>
          <dl className="mt-2 grid grid-cols-[68px_minmax(0,1fr)] gap-x-2 gap-y-1.5 type-caption">
            {rows.map(([label, value]) => (
              <div key={label} className="contents">
                <dt className="text-[var(--fg-3)]">{label}</dt>
                <dd className="min-w-0 break-words text-[var(--fg-1)]">
                  {value}
                </dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </div>
  );
}

function executionTaskRows(
  task: CanvasExecutionTaskDetail,
  elapsed: string | null,
): Array<[string, string]> {
  const taskId =
    task.video_generation_id ??
    task.generation_id ??
    task.completion_id ??
    task.id;
  const provider = [task.provider_name, task.provider_kind]
    .filter(Boolean)
    .join(" · ");
  const output = [
    task.resolution,
    task.duration_s != null ? `${task.duration_s} 秒` : null,
    task.aspect_ratio,
    task.size_requested,
  ]
    .filter(Boolean)
    .join(" · ");
  const rows: Array<[string, string]> = [
    ["任务 ID", taskId],
    ["类型", canvasTaskKindLabel(task.kind)],
    ["模型", task.model ?? ""],
    ["供应商", provider],
    ["模式", canvasTaskActionLabel(task.action)],
    ["规格", output],
    [
      "音频",
      task.generate_audio == null
        ? ""
        : task.generate_audio
          ? "生成音频"
          : "静音",
    ],
    ["尝试", task.attempt == null ? "" : String(task.attempt + 1)],
    ["耗时", elapsed ?? ""],
    ["更新时间", formatCanvasTaskTime(task.updated_at)],
  ];
  return rows.filter((row) => Boolean(row[1]));
}

function canvasTaskKindLabel(kind: string): string {
  return (
    {
      generation: "图片生成",
      completion: "文本处理",
      video_generation: "视频生成",
    }[kind] ?? kind
  );
}

function canvasTaskActionLabel(action: string | null | undefined): string {
  if (!action) return "";
  return (
    {
      t2v: "文生视频",
      i2v: "图生视频",
      reference: "参考生成",
      generate: "生成",
      edit: "编辑",
    }[action] ?? action
  );
}

function formatCanvasTaskTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN");
}
