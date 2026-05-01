"use client";

import {
  ButtonHTMLAttributes,
  AnchorHTMLAttributes,
  forwardRef,
  useCallback,
  useState,
  type PointerEvent,
} from "react";
import { useHaptic, type HapticKind } from "@/hooks/useHaptic";
import { PRESS_SCALE, type PressScaleName } from "@/lib/motion";

type BaseProps = {
  /** false | haptic kind；默认 "light" */
  haptic?: false | HapticKind;
  /** 按压缩放强度；默认 "tight"（小按钮/icon） */
  pressScale?: PressScaleName;
  /** 是否强制 min-w/h = 44px（iOS HIG）；默认 true */
  minHit?: boolean;
  /** inline: 文本内按钮不强制 44px；default / large：44 / 56 */
  size?: "inline" | "default" | "large";
  disabled?: boolean;
  children: React.ReactNode;
  className?: string;
};

type AsButton = BaseProps &
  Omit<ButtonHTMLAttributes<HTMLButtonElement>, keyof BaseProps | "type"> & {
    as?: "button";
    onPress?: (e: PointerEvent<HTMLButtonElement>) => void;
  };

type AsAnchor = BaseProps &
  Omit<AnchorHTMLAttributes<HTMLAnchorElement>, keyof BaseProps> & {
    as: "a";
    href: string;
    onPress?: (e: PointerEvent<HTMLAnchorElement>) => void;
  };

export type PressableProps = AsButton | AsAnchor;

/**
 * 统一按压反馈模板。对外屏蔽 scale / opacity / haptic / focus ring 细节。
 * 行为：
 *  - pointerdown → data-pressed=true + haptic
 *  - pointerup / cancel / leave → data-pressed=false
 *  - disabled → opacity-40 + 禁用 haptic + 禁用 onPress
 *  - size: inline 不强制命中区；default = h-11 min-w-11；large = h-14
 *  - focus-visible → box-shadow var(--ring)
 */
export const Pressable = forwardRef<HTMLElement, PressableProps>(function Pressable(
  props,
  ref,
) {
  const {
    haptic: hapticKind = "light",
    pressScale = "tight",
    minHit = true,
    size = "default",
    disabled = false,
    className = "",
    children,
    onPress,
    ...rest
  } = props as BaseProps & { onPress?: (e: PointerEvent<HTMLElement>) => void; as?: "button" | "a" };

  const [pressed, setPressed] = useState(false);
  const { haptic } = useHaptic();
  const as = (props as { as?: "button" | "a" }).as ?? "button";

  const onPointerDown = useCallback(
    () => {
      if (disabled) return;
      setPressed(true);
      if (hapticKind !== false) haptic(hapticKind);
    },
    [disabled, haptic, hapticKind],
  );

  const clearPressed = useCallback(() => setPressed(false), []);

  const handlePointerUp = useCallback(
    (e: PointerEvent<HTMLElement>) => {
      clearPressed();
      if (disabled) return;
      onPress?.(e);
    },
    [clearPressed, disabled, onPress],
  );

  const scale = disabled ? 1 : (pressed ? PRESS_SCALE[pressScale] : 1);
  const opacity = disabled ? "var(--op-disabled)" : (pressed ? "var(--op-press)" : 1);

  // 命中区规则：inline 不加 min；default = 44；large = 56。minHit=false 可强制关掉。
  const hitClasses =
    size === "inline" || minHit === false
      ? ""
      : size === "large"
        ? "min-h-14 min-w-14"
        : "min-h-11 min-w-11";

  const baseClasses = [
    "relative inline-flex items-center justify-center select-none touch-manipulation",
    "transition-transform transition-opacity duration-[var(--dur-instant)] ease-[var(--ease-shutter)]",
    "disabled:cursor-not-allowed focus-visible:outline-none",
    hitClasses,
    className,
  ]
    .filter(Boolean)
    .join(" ");

  const commonStyle = {
    transform: `scale(${scale})`,
    opacity,
  } as React.CSSProperties;

  if (as === "a") {
    const anchorProps = rest as AnchorHTMLAttributes<HTMLAnchorElement>;
    return (
      <a
        ref={ref as React.Ref<HTMLAnchorElement>}
        data-pressed={pressed || undefined}
        aria-disabled={disabled || undefined}
        className={baseClasses}
        style={commonStyle}
        onPointerDown={onPointerDown as (e: PointerEvent<HTMLAnchorElement>) => void}
        onPointerUp={handlePointerUp as (e: PointerEvent<HTMLAnchorElement>) => void}
        onPointerCancel={clearPressed}
        onPointerLeave={clearPressed}
        {...anchorProps}
      >
        {children}
      </a>
    );
  }

  const buttonProps = rest as ButtonHTMLAttributes<HTMLButtonElement>;
  return (
    <button
      ref={ref as React.Ref<HTMLButtonElement>}
      type="button"
      data-pressed={pressed || undefined}
      disabled={disabled}
      className={baseClasses}
      style={commonStyle}
      onPointerDown={onPointerDown as (e: PointerEvent<HTMLButtonElement>) => void}
      onPointerUp={handlePointerUp as (e: PointerEvent<HTMLButtonElement>) => void}
      onPointerCancel={clearPressed}
      onPointerLeave={clearPressed}
      {...buttonProps}
    >
      {children}
    </button>
  );
});
