import {
  Background,
  BackgroundVariant,
  MiniMap,
  ReactFlow,
  type Edge,
  type ReactFlowInstance,
  type ReactFlowProps,
} from "@xyflow/react";
import type {
  DragEvent,
  MutableRefObject,
  PointerEvent,
  RefObject,
  TouchEvent,
} from "react";

import type { ConnectionDraft } from "@/lib/canvas/types";
import { toast } from "@/components/ui/primitives";
import { CanvasViewportControls } from "./CanvasViewportControls";
import {
  canvasGridGap,
  fitCanvasViewport,
  viewportAnimationDuration,
  type CompatibleTarget,
  type CanvasViewportPreferences,
} from "./CanvasViewportModel";
import {
  CanvasEmptyState,
  MobileConnectTargets,
} from "./CanvasViewportOverlays";
import { canvasNodeTypes, type CanvasFlowNode } from "./nodes/CanvasNodes";
import styles from "./canvas.module.css";

interface CanvasViewportSurfaceProps {
  "aria-label": string;
  ariaLabelConfig: ReactFlowProps<
    CanvasFlowNode,
    Edge
  >["ariaLabelConfig"];
  connectOnClick: false;
  elevateNodesOnSelect: false;
  flowProps: ReactFlowProps<CanvasFlowNode, Edge>;
  onlyRenderVisibleElements: true;
  viewportRef: RefObject<HTMLDivElement | null>;
  onDrop: (event: DragEvent<HTMLDivElement>) => void;
  onPointerCancelCapture: () => void;
  onTouchCancelCapture: () => void;
  onPointerCancel: (event: PointerEvent<HTMLDivElement>) => void;
  onTouchCancel: (event: TouchEvent<HTMLDivElement>) => void;
  snapToGrid: boolean;
  snapGrid: [number, number];
  showMiniMap: boolean;
  isMobile: boolean;
  instance: ReactFlowInstance<CanvasFlowNode, Edge> | null;
  zoom: number;
  minimumZoom: number;
  maximumZoom: number;
  reducedMotion: boolean;
  isTablet: boolean;
  viewportPreferencesRef: MutableRefObject<CanvasViewportPreferences>;
  onGridVisibleChange: (visible: boolean) => void;
  miniMapVisible: boolean;
  onMiniMapVisibleChange: (visible: boolean) => void;
  nodeCount: number;
  onEmptyQuickAdd: () => void;
  connectionDraft: ConnectionDraft | null;
  targetPickerOpen: boolean;
  targets: CompatibleTarget[];
  onTargetPickerOpenChange: (open: boolean) => void;
  connectionDraftRef: MutableRefObject<ConnectionDraft | null>;
  addEdge: (input: {
    sourceNodeId: string;
    sourceHandle: string;
    targetNodeId: string;
    targetHandle: string;
  }) => { ok: true } | { ok: false; reason: string };
  updateConnectionDraft: (draft: ConnectionDraft | null) => void;
}

export function CanvasViewportSurface({
  "aria-label": ariaLabel,
  ariaLabelConfig,
  connectOnClick,
  elevateNodesOnSelect,
  flowProps,
  onlyRenderVisibleElements,
  viewportRef,
  onDrop,
  onPointerCancelCapture,
  onTouchCancelCapture,
  onPointerCancel,
  onTouchCancel,
  snapToGrid,
  snapGrid,
  showMiniMap,
  isMobile,
  instance,
  zoom,
  minimumZoom,
  maximumZoom,
  reducedMotion,
  isTablet,
  viewportPreferencesRef,
  onGridVisibleChange,
  miniMapVisible,
  onMiniMapVisibleChange,
  nodeCount,
  onEmptyQuickAdd,
  connectionDraft,
  targetPickerOpen,
  targets,
  onTargetPickerOpenChange,
  connectionDraftRef,
  addEdge,
  updateConnectionDraft,
}: CanvasViewportSurfaceProps) {
  return (
    <div
      ref={viewportRef}
      className={styles.viewport}
      onDrop={onDrop}
      onPointerCancelCapture={onPointerCancelCapture}
      onTouchCancelCapture={onTouchCancelCapture}
      onPointerCancel={onPointerCancel}
      onTouchCancel={onTouchCancel}
      onDragOver={(event) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "copy";
      }}
    >
      <ReactFlow<CanvasFlowNode, Edge>
        {...flowProps}
        aria-label={ariaLabel}
        nodeTypes={canvasNodeTypes}
        onlyRenderVisibleElements={onlyRenderVisibleElements}
        elevateNodesOnSelect={elevateNodesOnSelect}
        connectOnClick={connectOnClick}
        ariaLabelConfig={ariaLabelConfig}
      >
        {snapToGrid ? (
          <Background
            variant={BackgroundVariant.Dots}
            gap={canvasGridGap(snapGrid[0])}
            size={1}
            color="var(--border)"
          />
        ) : null}
        {showMiniMap ? (
          <MiniMap
            className={styles.miniMap}
            pannable
            zoomable
            nodeColor="var(--fg-2)"
            maskColor="color-mix(in srgb, var(--bg-0) 78%, transparent)"
          />
        ) : null}
      </ReactFlow>
      {!isMobile && instance ? (
        <CanvasViewportControls
          zoom={zoom}
          minZoom={minimumZoom}
          maxZoom={maximumZoom}
          onZoomOut={() => {
            void instance.zoomOut({
              duration: viewportAnimationDuration(reducedMotion),
            });
          }}
          onZoomIn={() => {
            void instance.zoomIn({
              duration: viewportAnimationDuration(reducedMotion),
            });
          }}
          onResetZoom={() => {
            void instance.zoomTo(1, {
              duration: viewportAnimationDuration(reducedMotion),
            });
          }}
          onFitView={() =>
            fitCanvasViewport(instance, viewportPreferencesRef.current)
          }
          showFitView={isTablet}
          gridVisible={snapToGrid}
          onGridVisibleChange={onGridVisibleChange}
          minimapVisible={miniMapVisible}
          onMinimapVisibleChange={onMiniMapVisibleChange}
          className="absolute bottom-3 left-3 z-[var(--z-tabbar)]"
        />
      ) : null}
      {nodeCount === 0 ? (
        <CanvasEmptyState onCreate={onEmptyQuickAdd} />
      ) : null}
      {isMobile && connectionDraft ? (
        <MobileConnectTargets
          open={targetPickerOpen}
          targets={targets}
          onOpen={() => onTargetPickerOpenChange(true)}
          onClose={() => onTargetPickerOpenChange(false)}
          onCancel={() => {
            onTargetPickerOpenChange(false);
            updateConnectionDraft(null);
          }}
          onSelect={(target) => {
            void instance?.setCenter(target.x, target.y, {
              zoom: 1,
              duration: viewportAnimationDuration(reducedMotion),
            });
            const draft = connectionDraftRef.current;
            if (!draft) return;
            const result = addEdge({
              sourceNodeId: draft.sourceNodeId,
              sourceHandle: draft.sourceHandle,
              targetNodeId: target.nodeId,
              targetHandle: target.handleId,
            });
            if (!result.ok) {
              toast.error(result.reason);
              return;
            }
            onTargetPickerOpenChange(false);
            updateConnectionDraft(null);
          }}
        />
      ) : null}
    </div>
  );
}
