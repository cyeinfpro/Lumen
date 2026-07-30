import {
  AlignHorizontalJustifyCenter,
  AlignHorizontalJustifyEnd,
  AlignHorizontalJustifyStart,
  AlignHorizontalSpaceBetween,
  AlignVerticalJustifyCenter,
  AlignVerticalJustifyEnd,
  AlignVerticalJustifyStart,
  AlignVerticalSpaceBetween,
  Copy,
  LayoutGrid,
  Scan,
  Trash2,
} from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { CANVAS_NODE_SPECS } from "@/lib/canvas/registry";
import type {
  CanvasNodeDefinition,
  CanvasNodeType,
} from "@/lib/canvas/types";
import {
  InspectorSection,
  InspectorShell,
} from "./CanvasInspectorFields";
import type {
  CanvasSelectionAlignment,
  CanvasSelectionDistribution,
} from "./CanvasInspectorContracts";

export function CanvasBatchInspector({
  nodes,
  onDuplicateSelection,
  onAlignSelection,
  onDistributeSelection,
  onAutoLayoutSelection,
  onFitSelection,
  onDeleteSelection,
}: {
  nodes: CanvasNodeDefinition[];
  onDuplicateSelection?: () => void;
  onAlignSelection?: (alignment: CanvasSelectionAlignment) => void;
  onDistributeSelection?: (
    distribution: CanvasSelectionDistribution,
  ) => void;
  onAutoLayoutSelection?: () => void;
  onFitSelection?: () => void;
  onDeleteSelection: () => void;
}) {
  const hasGeneralActions =
    onDuplicateSelection || onAutoLayoutSelection || onFitSelection;
  return (
    <InspectorShell eyebrow="批量检查器" title={`已选择 ${nodes.length} 个节点`}>
      <div className="mobile-dialog-scroll min-h-0 flex-1 overflow-y-auto">
        <InspectorSection title="选择摘要">
          <p className="type-body-sm text-[var(--fg-1)]">
            {selectionSummary(nodes)}
          </p>
        </InspectorSection>

        {hasGeneralActions ? (
          <InspectorSection title="批量操作">
            <div
              className="grid grid-cols-2 gap-2"
              role="group"
              aria-label="批量节点操作"
            >
              {onDuplicateSelection ? (
                <Button
                  size="sm"
                  variant="outline"
                  leftIcon={<Copy className="h-4 w-4" aria-hidden />}
                  onClick={onDuplicateSelection}
                >
                  复制节点
                </Button>
              ) : null}
              {onAutoLayoutSelection ? (
                <Button
                  size="sm"
                  variant="outline"
                  leftIcon={<LayoutGrid className="h-4 w-4" aria-hidden />}
                  onClick={onAutoLayoutSelection}
                >
                  自动布局
                </Button>
              ) : null}
              {onFitSelection ? (
                <Button
                  size="sm"
                  variant="outline"
                  leftIcon={<Scan className="h-4 w-4" aria-hidden />}
                  onClick={onFitSelection}
                >
                  适应选择
                </Button>
              ) : null}
            </div>
          </InspectorSection>
        ) : null}

        {onAlignSelection ? (
          <InspectorSection title="对齐">
            <div
              className="grid grid-cols-3 gap-2"
              role="group"
              aria-label="节点对齐方式"
            >
              <BatchLayoutButton
                label="左对齐"
                icon={<AlignHorizontalJustifyStart className="h-4 w-4" />}
                onClick={() => onAlignSelection("left")}
              />
              <BatchLayoutButton
                label="水平居中"
                icon={<AlignHorizontalJustifyCenter className="h-4 w-4" />}
                onClick={() => onAlignSelection("horizontal-center")}
              />
              <BatchLayoutButton
                label="右对齐"
                icon={<AlignHorizontalJustifyEnd className="h-4 w-4" />}
                onClick={() => onAlignSelection("right")}
              />
              <BatchLayoutButton
                label="顶部对齐"
                icon={<AlignVerticalJustifyStart className="h-4 w-4" />}
                onClick={() => onAlignSelection("top")}
              />
              <BatchLayoutButton
                label="垂直居中"
                icon={<AlignVerticalJustifyCenter className="h-4 w-4" />}
                onClick={() => onAlignSelection("vertical-center")}
              />
              <BatchLayoutButton
                label="底部对齐"
                icon={<AlignVerticalJustifyEnd className="h-4 w-4" />}
                onClick={() => onAlignSelection("bottom")}
              />
            </div>
          </InspectorSection>
        ) : null}

        {onDistributeSelection ? (
          <InspectorSection title="均匀分布">
            <div
              className="grid grid-cols-2 gap-2"
              role="group"
              aria-label="节点分布方式"
            >
              <Button
                size="sm"
                variant="outline"
                leftIcon={
                  <AlignHorizontalSpaceBetween
                    className="h-4 w-4"
                    aria-hidden
                  />
                }
                onClick={() => onDistributeSelection("horizontal")}
              >
                水平分布
              </Button>
              <Button
                size="sm"
                variant="outline"
                leftIcon={
                  <AlignVerticalSpaceBetween
                    className="h-4 w-4"
                    aria-hidden
                  />
                }
                onClick={() => onDistributeSelection("vertical")}
              >
                垂直分布
              </Button>
            </div>
          </InspectorSection>
        ) : null}
      </div>

      <footer className="mobile-dialog-footer shrink-0 border-t border-[var(--border)] bg-[var(--bg-1)]/92 p-3">
        <Button
          variant="danger"
          fullWidth
          leftIcon={<Trash2 className="h-4 w-4" aria-hidden />}
          onClick={onDeleteSelection}
        >
          删除所选
        </Button>
      </footer>
    </InspectorShell>
  );
}

function BatchLayoutButton({
  label,
  icon,
  onClick,
}: {
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <Button
      size="sm"
      variant="outline"
      className="min-w-0 px-2 text-[11px]"
      leftIcon={<span aria-hidden>{icon}</span>}
      onClick={onClick}
    >
      {label}
    </Button>
  );
}

function selectionSummary(nodes: CanvasNodeDefinition[]): string {
  const counts = new Map<CanvasNodeType, number>();
  for (const node of nodes) {
    counts.set(node.type, (counts.get(node.type) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([type, count]) => `${CANVAS_NODE_SPECS[type].label} ${count} 个`)
    .join("，");
}
