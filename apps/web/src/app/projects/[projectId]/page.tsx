"use client";

// 项目详情 dispatcher：按 workflow.type 切换到对应详情组件。
// - apparel_model_showcase → ApparelWorkflowDetail
// - poster_design          → PosterWorkflowDetail

import { use } from "react";

import { ApparelWorkflowDetail, PosterWorkflowDetail } from "@/components/ui/projects";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { useWorkflowQuery } from "@/lib/queries";

export default function ProjectDetailPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = use(params);
  return <ProjectDispatcher projectId={projectId} />;
}

function ProjectDispatcher({ projectId }: { projectId: string }) {
  const query = useWorkflowQuery(projectId);
  const workflow = query.data;

  if (!workflow && query.isLoading) {
    return (
      <div className="flex h-[100dvh] items-center justify-center bg-[var(--bg-0)]">
        <div className="grid place-items-center gap-3 text-center">
          <Spinner size={20} />
          <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-[var(--fg-2)]">
            加载中
          </p>
        </div>
      </div>
    );
  }

  if (workflow?.type === "poster_design") {
    return <PosterWorkflowDetail projectId={projectId} />;
  }

  // 默认 / apparel_model_showcase 走原详情组件，保持现有行为不变
  return <ApparelWorkflowDetail projectId={projectId} />;
}
