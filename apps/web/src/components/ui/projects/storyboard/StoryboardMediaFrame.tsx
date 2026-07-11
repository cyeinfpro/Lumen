import { Image as ImageIcon, type LucideIcon } from "lucide-react";
import Image from "next/image";

import { cn } from "@/lib/utils";

type StoryboardMediaFrameProps = {
  src: string | null | undefined;
  alt: string;
  className: string;
  emptyClassName: string;
  emptyIcon?: LucideIcon;
  emptyIconClassName?: string;
  sizes?: string;
};

export function StoryboardMediaFrame({
  src,
  alt,
  className,
  emptyClassName,
  emptyIcon: EmptyIcon = ImageIcon,
  emptyIconClassName = "h-6 w-6",
  sizes = "(max-width: 768px) 100vw, (max-width: 1280px) 50vw, 33vw",
}: StoryboardMediaFrameProps) {
  if (!src) {
    return (
      <div className={emptyClassName}>
        <EmptyIcon className={emptyIconClassName} />
      </div>
    );
  }

  return (
    <Image
      src={src}
      alt={alt}
      width={640}
      height={360}
      sizes={sizes}
      loading="lazy"
      decoding="async"
      unoptimized
      className={cn("object-cover", className)}
    />
  );
}
