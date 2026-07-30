import type {
  CanvasGraph,
  CanvasNodeDefinition,
} from "@/lib/canvas/types";
import type { VideoOptionsOut } from "@/lib/types";

export interface CanvasNodeConfigEditorProps {
  node: CanvasNodeDefinition;
  graph: CanvasGraph;
  patch: (next: Record<string, unknown>) => void;
  uploading: boolean;
  onUploadImage: (file: File) => Promise<void>;
  onUploadVideo: (file: File) => Promise<void>;
  videoOptions?: VideoOptionsOut;
  videoOptionsLoading?: boolean;
  videoOptionsError?: string | null;
  videoOptionsRetrying?: boolean;
  onRetryVideoOptions?: () => void;
}
