import { CanvasWorkspace } from "@/components/ui/canvas/CanvasWorkspace";

export default async function CanvasDetailPage({
  params,
}: {
  params: Promise<{ canvasId: string }>;
}) {
  const { canvasId } = await params;
  return <CanvasWorkspace canvasId={canvasId} />;
}
