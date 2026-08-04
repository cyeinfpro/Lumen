"use client";

import { cn } from "@/lib/utils";

export type AvatarSize = "sm" | "md" | "lg";

export interface AvatarProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "children"> {
  src?: string;
  alt?: string;
  name?: string;
  initials?: string;
  fallback?: React.ReactNode;
  children?: React.ReactNode;
  size?: AvatarSize;
}

const SIZES: Record<AvatarSize, string> = {
  sm: "h-8 w-8 type-caption",
  md: "h-10 w-10 type-body-sm",
  lg: "h-12 w-12 type-card-title",
};

function getInitials(name?: string) {
  if (!name) return "";
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0]}${parts[parts.length - 1][0]}`.toUpperCase();
}

export function Avatar({
  src,
  alt,
  name,
  initials,
  fallback,
  children,
  size = "md",
  className,
  ref,
  ...props
}: AvatarProps & { ref?: React.Ref<HTMLDivElement> }) {
  const fallbackContent = fallback ?? children ?? initials ?? getInitials(name);

  return (
    <div
      {...props}
      ref={ref}
      role={src ? undefined : "img"}
      aria-label={src ? undefined : alt ?? name ?? undefined}
      className={cn(
        "inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full " +
          "border border-[var(--border)] bg-[var(--bg-2)] text-[var(--fg-0)] select-none",
        SIZES[size],
        className,
      )}
    >
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={src}
          alt={alt ?? name ?? ""}
          className="h-full w-full object-cover"
        />
      ) : (
        fallbackContent
      )}
    </div>
  );
}
