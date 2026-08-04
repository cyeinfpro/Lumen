"use client";

import { cn } from "@/lib/utils";

export type SliderProps = Omit<
  React.InputHTMLAttributes<HTMLInputElement>,
  "type"
>;

const RANGE =
  "h-6 w-full cursor-pointer appearance-none bg-transparent touch-manipulation " +
  "focus-visible:outline-none focus-visible:shadow-[var(--ring)] " +
  "disabled:cursor-not-allowed disabled:opacity-50 max-sm:min-h-11 " +
  "[&::-webkit-slider-runnable-track]:h-1.5 " +
  "[&::-webkit-slider-runnable-track]:rounded-full " +
  "[&::-webkit-slider-runnable-track]:bg-[var(--bg-3)] " +
  "[&::-webkit-slider-thumb]:-mt-1 " +
  "[&::-webkit-slider-thumb]:h-4 " +
  "[&::-webkit-slider-thumb]:w-4 " +
  "[&::-webkit-slider-thumb]:appearance-none " +
  "[&::-webkit-slider-thumb]:rounded-full " +
  "[&::-webkit-slider-thumb]:border-0 " +
  "[&::-webkit-slider-thumb]:bg-accent " +
  "[&::-moz-range-track]:h-1.5 " +
  "[&::-moz-range-track]:rounded-full " +
  "[&::-moz-range-track]:bg-[var(--bg-3)] " +
  "[&::-moz-range-thumb]:h-4 " +
  "[&::-moz-range-thumb]:w-4 " +
  "[&::-moz-range-thumb]:rounded-full " +
  "[&::-moz-range-thumb]:border-0 " +
  "[&::-moz-range-thumb]:bg-accent";

export function Slider({
  className,
  ref,
  ...props
}: SliderProps & { ref?: React.Ref<HTMLInputElement> }) {
  return (
    <input
      {...props}
      ref={ref}
      type="range"
      className={cn(RANGE, className)}
    />
  );
}
