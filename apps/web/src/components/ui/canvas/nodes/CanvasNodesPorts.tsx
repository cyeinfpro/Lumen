import { Handle, Position } from "@xyflow/react";

import type { CanvasPortSpec } from "@/lib/canvas/registry";
import type { CanvasDataType } from "@/lib/canvas/types";
import { cn } from "@/lib/utils";
import styles from "../canvas.module.css";

export function NodePorts({
  ports,
  direction,
  connectionType,
  compatibleHandles = [],
  onStartConnection,
}: {
  ports: CanvasPortSpec[];
  direction: "input" | "output";
  connectionType?: CanvasDataType | null;
  compatibleHandles?: string[];
  onStartConnection?: (port: CanvasPortSpec) => void;
}) {
  return ports.map((port, index) => {
    const compatible =
      direction === "input" &&
      Boolean(connectionType) &&
      compatibleHandles.includes(port.id);
    const top = `${((index + 1) / (ports.length + 1)) * 100}%`;
    return (
      <Handle
        key={port.id}
        id={port.id}
        type={direction === "input" ? "target" : "source"}
        position={direction === "input" ? Position.Left : Position.Right}
        isConnectableStart={direction === "output"}
        isConnectableEnd={direction === "input"}
        style={{ top }}
        data-port-type={port.dataType}
        aria-label={`${direction === "input" ? "输入" : "输出"}端口 ${port.label} ${port.dataType}`}
        aria-keyshortcuts={onStartConnection ? "Enter Space" : undefined}
        title={`${port.label} · ${port.dataType}`}
        role={onStartConnection ? "button" : undefined}
        tabIndex={onStartConnection ? 0 : -1}
        onClick={
          onStartConnection
            ? (event) => {
                event.stopPropagation();
                onStartConnection(port);
              }
            : undefined
        }
        onKeyDown={
          onStartConnection
            ? (event) => {
                if (event.key !== "Enter" && event.key !== " ") return;
                event.preventDefault();
                event.stopPropagation();
                onStartConnection(port);
              }
            : undefined
        }
        className={cn(
          styles.handle,
          "nokey touch-manipulation after:absolute after:-inset-4 after:content-[''] focus-visible:outline-none focus-visible:shadow-[var(--ring)]",
          compatible && styles.handleCompatible,
        )}
      />
    );
  });
}
