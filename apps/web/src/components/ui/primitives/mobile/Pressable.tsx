"use client";

import {
  ButtonHTMLAttributes,
  AnchorHTMLAttributes,
  forwardRef,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useReducedMotion } from "framer-motion";
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

// onPress 不带 event 参数：键盘 (Enter/Space) / 鼠标 click / 触屏 / 屏幕阅读器
// 都能触发。需要 event 信息的场景请直接用 onClick / onKeyDown。
type PressHandler = () => void;

type AsButton = BaseProps &
  Omit<ButtonHTMLAttributes<HTMLButtonElement>, keyof BaseProps | "type"> & {
    as?: "button";
    onPress?: PressHandler;
  };

type AsAnchor = BaseProps &
  Omit<AnchorHTMLAttributes<HTMLAnchorElement>, keyof BaseProps> & {
    as: "a";
    href: string;
    onPress?: PressHandler;
  };

export type PressableProps = AsButton | AsAnchor;

function pressableVisualState(
  disabled: boolean,
  pressed: boolean,
  pressScale: PressScaleName,
  reduceMotion: boolean | null,
) {
  if (disabled) {
    return { scale: 1, opacity: "var(--op-disabled)" };
  }
  if (pressed) {
    return {
      scale: reduceMotion ? 1 : PRESS_SCALE[pressScale],
      opacity: "var(--op-press)",
    };
  }
  return { scale: 1, opacity: undefined };
}

function pressableHitClasses(
  size: BaseProps["size"],
  minHit: boolean,
): string {
  if (size === "inline" || !minHit) return "";
  return size === "large"
    ? "min-h-14 min-w-14"
    : "min-h-11 min-w-11";
}

/**
 * 统一按压反馈模板。对外屏蔽 scale / opacity / haptic / focus ring 细节。
 * 行为：
 *  - pointerdown → data-pressed=true + haptic（仅物理反馈，不触发 onPress）
 *  - pointerup / cancel / leave → data-pressed=false
 *  - onPress 触发：原生 click 事件（覆盖鼠标 / 触屏 / 屏幕阅读器 / button 键盘 Enter+Space）
 *  - 额外：as="a" 时 keydown Space 触发 onPress（anchor 默认只响应 Enter）
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
  } = props as BaseProps & { onPress?: PressHandler; as?: "button" | "a" };

  const [pressed, setPressed] = useState(false);
  const activePointerRef = useRef<number | null>(null);
  const { haptic } = useHaptic();
  const reduceMotion = useReducedMotion();
  const as = (props as { as?: "button" | "a" }).as ?? "button";

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLElement>) => {
      if (
        disabled ||
        !event.isPrimary ||
        (event.pointerType === "mouse" && event.button !== 0)
      ) {
        return;
      }
      activePointerRef.current = event.pointerId;
      setPressed(true);
      if (hapticKind !== false) haptic(hapticKind);
    },
    [disabled, haptic, hapticKind],
  );

  const releasePointer = useCallback(
    (event: React.PointerEvent<HTMLElement>) => {
      if (activePointerRef.current !== event.pointerId) return;
      activePointerRef.current = null;
      setPressed(false);
    },
    [],
  );
  const leavePointer = useCallback(
    (event: React.PointerEvent<HTMLElement>) => {
      if (activePointerRef.current === event.pointerId) setPressed(false);
    },
    [],
  );
  const enterPointer = useCallback(
    (event: React.PointerEvent<HTMLElement>) => {
      if (
        activePointerRef.current === event.pointerId &&
        event.buttons !== 0
      ) {
        setPressed(true);
      }
    },
    [],
  );

  useEffect(() => {
    if (!disabled) return;
    activePointerRef.current = null;
    setPressed(false);
  }, [disabled]);

  // onPress 桥接：原生 click 已覆盖鼠标/触屏/屏幕阅读器 + button 键盘 Enter/Space。
  // pointerup 仅清按压 state，不再触发 onPress（避免双触发）。
  const handleClick = useCallback(() => {
    if (disabled) return;
    onPress?.();
  }, [disabled, onPress]);

  // anchor 默认只响应 Enter；Space 默认不触发 click，需手动桥接。button 跳过。
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLElement>) => {
      if (disabled || as !== "a") return;
      if (e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        onPress?.();
      }
    },
    [as, disabled, onPress],
  );

  const { scale, opacity } = pressableVisualState(
    disabled,
    pressed,
    pressScale,
    reduceMotion,
  );

  // 命中区规则：inline 不加 min；default = 44；large = 56。minHit=false 可强制关掉。
  const hitClasses = pressableHitClasses(size, minHit);

  const baseClasses = [
    "relative inline-flex items-center justify-center select-none touch-manipulation",
    "transition-transform transition-opacity duration-[var(--dur-instant)] ease-[var(--ease-shutter)]",
    "disabled:cursor-not-allowed focus-visible:outline-none",
    hitClasses,
    className,
  ]
    .filter(Boolean)
    .join(" ");

  const pressStyle = {
    transform: `scale(${scale})`,
    opacity,
  } as React.CSSProperties;

  const mergedStyle = (style?: React.CSSProperties): React.CSSProperties => {
    const nextStyle: React.CSSProperties = {
      ...style,
      transform: [style?.transform, pressStyle.transform]
        .filter(Boolean)
        .join(" "),
    };
    if (pressStyle.opacity !== undefined) {
      nextStyle.opacity = pressStyle.opacity;
    }
    return nextStyle;
  };

  if (as === "a") {
    const {
      style,
      onClick,
      onKeyDown,
      onPointerDown: onPointerDownProp,
      onPointerUp,
      onPointerCancel,
      onPointerLeave,
      onPointerEnter,
      onBlur,
      tabIndex,
      ...anchorProps
    } = rest as AnchorHTMLAttributes<HTMLAnchorElement>;
    return (
      <a
        ref={ref as React.Ref<HTMLAnchorElement>}
        data-pressed={pressed || undefined}
        aria-disabled={disabled || undefined}
        tabIndex={disabled ? -1 : tabIndex}
        className={baseClasses}
        style={mergedStyle(style)}
        onPointerDown={(event) => {
          onPointerDownProp?.(event);
          if (!event.defaultPrevented) onPointerDown(event);
        }}
        onPointerUp={(event) => {
          onPointerUp?.(event);
          releasePointer(event);
        }}
        onPointerCancel={(event) => {
          onPointerCancel?.(event);
          releasePointer(event);
        }}
        onPointerLeave={(event) => {
          onPointerLeave?.(event);
          leavePointer(event);
        }}
        onPointerEnter={(event) => {
          onPointerEnter?.(event);
          if (!event.defaultPrevented) enterPointer(event);
        }}
        onClick={(event) => {
          if (disabled) {
            event.preventDefault();
            return;
          }
          onClick?.(event);
          if (!event.defaultPrevented) handleClick();
        }}
        onKeyDown={(event) => {
          if (
            disabled &&
            (event.key === "Enter" ||
              event.key === " " ||
              event.key === "Spacebar")
          ) {
            event.preventDefault();
            return;
          }
          onKeyDown?.(event);
          if (!event.defaultPrevented) handleKeyDown(event);
        }}
        onBlur={(event) => {
          onBlur?.(event);
          activePointerRef.current = null;
          setPressed(false);
        }}
        {...anchorProps}
      >
        {children}
      </a>
    );
  }

  const {
    style,
    onClick,
    onPointerDown: onPointerDownProp,
    onPointerUp,
    onPointerCancel,
    onPointerLeave,
    onPointerEnter,
    onBlur,
    ...buttonProps
  } = rest as ButtonHTMLAttributes<HTMLButtonElement>;
  return (
    <button
      ref={ref as React.Ref<HTMLButtonElement>}
      type="button"
      data-pressed={pressed || undefined}
      disabled={disabled}
      className={baseClasses}
      style={mergedStyle(style)}
      onPointerDown={(event) => {
        onPointerDownProp?.(event);
        if (!event.defaultPrevented) onPointerDown(event);
      }}
      onPointerUp={(event) => {
        onPointerUp?.(event);
        releasePointer(event);
      }}
      onPointerCancel={(event) => {
        onPointerCancel?.(event);
        releasePointer(event);
      }}
      onPointerLeave={(event) => {
        onPointerLeave?.(event);
        leavePointer(event);
      }}
      onPointerEnter={(event) => {
        onPointerEnter?.(event);
        if (!event.defaultPrevented) enterPointer(event);
      }}
      onClick={(event) => {
        onClick?.(event);
        if (!event.defaultPrevented) handleClick();
      }}
      onBlur={(event) => {
        onBlur?.(event);
        activePointerRef.current = null;
        setPressed(false);
      }}
      {...buttonProps}
    >
      {children}
    </button>
  );
});
