"use client";

import { use } from "react";

import { StoryboardDetailPage } from "@/components/ui/projects";

export default function Page({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = use(params);
  return (
    <div className="min-h-0 min-w-0 flex-1">
      <StoryboardDetailPage storyboardId={runId} />
    </div>
  );
}
