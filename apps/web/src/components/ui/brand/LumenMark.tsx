import { cn } from "@/lib/utils";

export function LumenMark({
  className,
  active = false,
}: {
  className?: string;
  active?: boolean;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      aria-hidden="true"
      className={cn("h-5 w-5", className)}
      fill="none"
    >
      <path
        d="M12 2.75a9.25 9.25 0 1 0 8.05 13.8"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <path
        d="M20.2 7.35 15.8 4.8l-4.4 2.55v5.1L15.8 15l4.4-2.55v-5.1Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <circle
        cx="15.8"
        cy="9.9"
        r="1.7"
        fill="currentColor"
        className={active ? "opacity-100" : "opacity-70"}
      />
    </svg>
  );
}
