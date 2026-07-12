"use client";

// 项目详情 dispatcher：按 workflow.type 切换到对应详情组件。
// - apparel_model_showcase → ApparelWorkflowDetail
// - poster_design          → PosterWorkflowDetail

import { useRouter, useSearchParams } from "next/navigation";
import { use, useEffect } from "react";

import { ApparelWorkflowDetail, PosterWorkflowDetail } from "@/components/ui/projects";
import { Spinner } from "@/components/ui/primitives/Spinner";
import { useWorkflowQuery } from "@/lib/queries";

export default function ProjectDetailPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = use(params);
  return (
    <div className="min-h-0 min-w-0 flex-1">
      <ProjectDispatcher projectId={projectId} />
    </div>
  );
}

function ProjectDispatcher({ projectId }: { projectId: string }) {
  const query = useWorkflowQuery(projectId);
  const workflow = query.data;
  const router = useRouter();
  const searchParams = useSearchParams();
  const search = searchParams.toString();

  useEffect(() => {
    if (workflow?.type === "storyboard") {
      router.replace(
        `/projects/storyboard/${projectId}${search ? `?${search}` : ""}`,
      );
    }
  }, [projectId, router, search, workflow?.type]);

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

  if (workflow?.type === "storyboard") {
    return null;
  }

  if (workflow?.type === "apparel_model_showcase") {
    return <ApparelWorkflowDetail projectId={projectId} />;
  }

  return (
    <div className="grid h-[100dvh] place-items-center bg-[var(--bg-0)] p-6 text-center text-[var(--fg-0)]">
      <div className="max-w-sm">
        <p className="text-sm text-[var(--fg-1)]">
          {query.isError ? "项目加载失败" : "暂不支持此项目类型"}
        </p>
        <button
          type="button"
          onClick={() => query.refetch()}
          className="mt-3 min-h-11 rounded-[var(--radius-control)] border border-[var(--border)] px-4 text-sm hover:bg-[var(--bg-1)]"
        >
          重试
        </button>
      </div>
    </div>
  );
}
