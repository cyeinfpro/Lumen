"use client";

import { use } from "react";

import { ApparelWorkflowDetail } from "@/components/ui/projects";

export default function ProjectDetailPage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = use(params);
  return <ApparelWorkflowDetail projectId={projectId} />;
}
