import { Suspense } from "react";
import { ModelLibraryPage } from "@/components/ui/projects/library";

export const metadata = {
  title: "模特库 · Lumen",
};

export default function Page() {
  return (
    <Suspense fallback={null}>
      <ModelLibraryPage />
    </Suspense>
  );
}
