import { Suspense } from "react";
import { PosterStylePage } from "@/components/ui/poster-styles";

export const metadata = {
  title: "风格库 · Lumen",
};

export default function Page() {
  return (
    <Suspense fallback={null}>
      <PosterStylePage />
    </Suspense>
  );
}
