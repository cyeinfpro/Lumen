import { cookies, headers } from "next/headers";

import { ResponsiveStudio } from "@/components/ui/shell/ResponsiveStudio";

function mobileUserAgent(value: string): boolean {
  return /Android|iPhone|iPad|iPod|Mobile/i.test(value);
}

export default async function Page() {
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

  return <ResponsiveStudio initialMobile={initialMobile} />;
}
