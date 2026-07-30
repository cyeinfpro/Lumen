import { Play, Trash2 } from "lucide-react";

import { Button, Input } from "@/components/ui/primitives";
import { CANVAS_NODE_TITLE_MAX_CHARS } from "@/lib/canvas/constants";
import type { CanvasNodeConfigEditorProps } from "./CanvasNodeConfigEditor";
import { CanvasNodeConfigEditor } from "./CanvasNodeConfigEditor";
import {
  ColorSwatchField,
  InlineConfigConfirmation,
  InspectorSection,
  InspectorShell,
  ToggleField,
} from "./CanvasInspectorFields";

interface PendingConfigChange {
  removedConnections: number;
}

export function CanvasInspectorNodePanel({
  node,
  eyebrow,
  graph,
  patch,
  uploading,
  onUploadImage,
  onUploadVideo,
  videoOptions,
  videoOptionsLoading,
  videoOptionsError,
  videoOptionsRetrying,
  onRetryVideoOptions,
  pendingConfigChange,
  history,
  canRun,
  runDisabledReason,
  running,
  onCommitTitle,
  onToggleCollapsed,
  onChangeColorTag,
  onCancelPendingChange,
  onConfirmPendingChange,
  onDelete,
  onRun,
}: Pick<
  CanvasNodeConfigEditorProps,
  | "node"
  | "graph"
  | "patch"
  | "uploading"
  | "onUploadImage"
  | "onUploadVideo"
  | "videoOptions"
  | "videoOptionsLoading"
  | "videoOptionsError"
  | "videoOptionsRetrying"
  | "onRetryVideoOptions"
> & {
  eyebrow: string;
  pendingConfigChange: PendingConfigChange | null;
  history: React.ReactNode;
  canRun: boolean;
  runDisabledReason: string | null;
  running: boolean;
  onCommitTitle: (value: string) => string;
  onToggleCollapsed: (collapsed: boolean) => void;
  onChangeColorTag: (colorTag: string | null) => void;
  onCancelPendingChange: () => void;
  onConfirmPendingChange: () => void;
  onDelete: () => void;
  onRun: () => void;
}) {
  return (
    <InspectorShell eyebrow={eyebrow} title={node.title}>
      <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto">
        <InspectorSection title="节点">
          <Input
            label="名称"
            defaultValue={node.title}
            key={`${node.id}:${node.title}`}
            maxLength={CANVAS_NODE_TITLE_MAX_CHARS}
            onBlur={(event) => {
              event.currentTarget.value = onCommitTitle(
                event.currentTarget.value,
              );
            }}
          />
          <ToggleField
            label="折叠节点"
            checked={node.ui.collapsed === true}
            onChange={onToggleCollapsed}
          />
          <ColorSwatchField
            value={node.ui.color_tag ?? null}
            onChange={onChangeColorTag}
          />
        </InspectorSection>

        <CanvasNodeConfigEditor
          node={node}
          graph={graph}
          patch={patch}
          uploading={uploading}
          onUploadImage={onUploadImage}
          onUploadVideo={onUploadVideo}
          videoOptions={videoOptions}
          videoOptionsLoading={videoOptionsLoading}
          videoOptionsError={videoOptionsError}
          videoOptionsRetrying={videoOptionsRetrying}
          onRetryVideoOptions={onRetryVideoOptions}
        />

        {pendingConfigChange ? (
          <InlineConfigConfirmation
            removedConnections={pendingConfigChange.removedConnections}
            onCancel={onCancelPendingChange}
            onConfirm={onConfirmPendingChange}
          />
        ) : null}

        {history}
      </div>

      <footer className="mobile-dialog-footer grid shrink-0 gap-2 border-t border-[var(--border)] bg-[var(--bg-1)]/92 p-3">
        {runDisabledReason ? (
          <p role="alert" className="type-caption text-[var(--danger-fg)]">
            {runDisabledReason}
          </p>
        ) : null}
        <div className="flex items-center justify-between gap-3">
          <Button
            variant="ghost"
            leftIcon={<Trash2 className="h-4 w-4" />}
            onClick={onDelete}
            className="text-[var(--danger-fg)] hover:bg-[var(--danger-soft)]"
          >
            删除
          </Button>
          {canRun ? (
            <Button
              variant="primary"
              loading={running}
              disabled={Boolean(runDisabledReason)}
              leftIcon={<Play className="h-4 w-4" />}
              onClick={onRun}
            >
              运行节点
            </Button>
          ) : (
            <Button variant="secondary" disabled>
              无需运行
            </Button>
          )}
        </div>
      </footer>
    </InspectorShell>
  );
}
