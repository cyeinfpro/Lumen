export type AssetVariantKind =
  | "thumb256"
  | "preview1024"
  | "display2048";

export type AssetVariantState = "ready" | "pending" | "missing" | "failed";

export interface AssetCandidate {
  kind: AssetVariantKind;
  src: string;
  width: number;
  ready: boolean;
}

export interface AssetCandidateImage {
  id: string;
  thumb_url?: string | null;
  preview_url?: string | null;
  display_url?: string | null;
  variant_version?: string | null;
  variants?: Partial<Record<AssetVariantKind, AssetVariantState>> | null;
  thumb_ready?: boolean | null;
  preview_ready?: boolean | null;
  display_ready?: boolean | null;
}

function normalizedSource(source: string | null | undefined): string | null {
  const value = source?.trim();
  return value ? value : null;
}

function gridSafeSource(source: string): boolean {
  const path = source.split(/[?#]/, 1)[0]?.replace(/\/+$/, "") ?? source;
  return !path.endsWith("/binary") && !path.includes("/variants/display2048");
}

function explicitReady(
  image: AssetCandidateImage,
  kind: AssetVariantKind,
): boolean | null {
  const state = image.variants?.[kind];
  if (state) return state === "ready";
  if (kind === "thumb256" && image.thumb_ready != null) {
    return image.thumb_ready;
  }
  if (kind === "preview1024" && image.preview_ready != null) {
    return image.preview_ready;
  }
  if (kind === "display2048" && image.display_ready != null) {
    return image.display_ready;
  }
  return null;
}

function candidate(
  image: AssetCandidateImage,
  kind: AssetVariantKind,
  source: string | null | undefined,
  width: number,
): AssetCandidate | null {
  const src = normalizedSource(source);
  if (!src) return null;
  const ready = explicitReady(image, kind);
  return {
    kind,
    src,
    width,
    // Old feed responses did not expose readiness. Existing thumb/preview URLs
    // remain compatible, while display readiness must be explicit for hover.
    ready: ready ?? kind !== "display2048",
  };
}

export function gridAssetCandidates(
  image: AssetCandidateImage,
): AssetCandidate[] {
  const candidates = [
    candidate(image, "thumb256", image.thumb_url, 256),
    candidate(image, "preview1024", image.preview_url, 1024),
  ];
  const seen = new Set<string>();
  return candidates
    .filter((entry): entry is AssetCandidate => Boolean(entry?.ready))
    .filter((entry) => gridSafeSource(entry.src))
    .filter((entry) => {
      if (seen.has(entry.src)) return false;
      seen.add(entry.src);
      return true;
    });
}

export function readyDisplayCandidate(
  image: AssetCandidateImage,
): AssetCandidate | null {
  const display = candidate(
    image,
    "display2048",
    image.display_url,
    2048,
  );
  return display?.ready ? display : null;
}

export function healthyAssetCandidates(
  candidates: AssetCandidate[],
  failedSources: ReadonlySet<string>,
): AssetCandidate[] {
  return candidates.filter(
    (entry) => entry.ready && !failedSources.has(entry.src),
  );
}

export function assetCandidateSrcSet(
  candidates: AssetCandidate[],
): string | undefined {
  const value = candidates.map((entry) => `${entry.src} ${entry.width}w`);
  return value.length > 0 ? value.join(", ") : undefined;
}

export function assetCandidateVersion(image: AssetCandidateImage): string {
  return [
    image.id,
    image.variant_version ?? "",
    image.thumb_url ?? "",
    image.preview_url ?? "",
    image.display_url ?? "",
    image.variants?.thumb256 ?? "",
    image.variants?.preview1024 ?? "",
    image.variants?.display2048 ?? "",
    image.thumb_ready == null ? "" : String(image.thumb_ready),
    image.preview_ready == null ? "" : String(image.preview_ready),
    image.display_ready == null ? "" : String(image.display_ready),
  ].join("|");
}

export function failedAssetCandidateSource(
  candidates: AssetCandidate[],
  failedSource: string | null | undefined,
  baseUrl?: string,
): string | null {
  const source = normalizedSource(failedSource);
  if (!source) return null;
  const canonical = (value: string): string => {
    try {
      return new URL(value, baseUrl).href;
    } catch {
      return value;
    }
  };
  const failedCanonical = canonical(source);
  return (
    candidates.find(
      (candidate) => canonical(candidate.src) === failedCanonical,
    )?.src ?? null
  );
}

export function hoverPrewarmCandidates(
  image: AssetCandidateImage,
): AssetCandidate[] {
  return gridAssetCandidates(image).filter(
    (entry) => entry.kind === "preview1024",
  );
}

export function confirmedOpenCandidates(
  image: AssetCandidateImage,
): AssetCandidate[] {
  const preview = candidate(
    image,
    "preview1024",
    image.preview_url,
    1024,
  );
  const display = candidate(
    image,
    "display2048",
    image.display_url,
    2048,
  );
  const seen = new Set<string>();
  return [display, preview]
    .filter((entry): entry is AssetCandidate => Boolean(entry))
    .filter((entry) => {
      if (seen.has(entry.src)) return false;
      seen.add(entry.src);
      return true;
    });
}
