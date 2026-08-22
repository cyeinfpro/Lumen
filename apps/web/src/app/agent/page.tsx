import { Suspense } from "react";
import { cookies, headers } from "next/headers";
import { ResponsiveAgent } from "@/features/agent";
import { Spinner } from "@/components/ui/primitives";

function mobileUserAgent(value: string): boolean {
  return /Android|iPhone|iPad|iPod|Mobile/i.test(value);
}

export default async function AgentPage() {
  const [cookieStore, headerStore] = await Promise.all([cookies(), headers()]);
  const rememberedViewport = cookieStore.get("lumen.viewport")?.value;
  const mobileClientHint = headerStore.get("sec-ch-ua-mobile");
  const initialMobile =
    rememberedViewport === "mobile"
      ? true
      : rememberedViewport === "desktop"
        ? false
        : mobileClientHint === "?1" ||
          mobileUserAgent(headerStore.get("user-agent") ?? "");

  return (
    <Suspense
      fallback={
        <div className="flex min-h-[100dvh] items-center justify-center bg-[var(--bg-0)]">
          <span role="status" className="flex items-center gap-2 type-body-sm text-[var(--fg-2)]">
            <Spinner size={20} /> 加载中
          </span>
        </div>
      }
    >
      <ResponsiveAgent initialMobile={initialMobile} />
    </Suspense>
  );
}
