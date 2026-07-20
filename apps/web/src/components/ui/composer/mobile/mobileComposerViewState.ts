import { MAX_PROMPT_CHARS } from "@/lib/promptLimits";
import {
  anyFlag,
  selectValue,
} from "../shared/composerViewState";

export function shouldShowPromptCount(
  text: string,
  promptTooLong: boolean,
): boolean {
  return anyFlag(text.length > MAX_PROMPT_CHARS * 0.8, promptTooLong);
}

export function deriveMobileComposerLayout(
  keyboardInset: number,
  viewportHeight: number,
) {
  const keyboardOffset = selectValue(keyboardInset > 60, keyboardInset, 0);
  const visibleViewportHeight = selectValue(
    viewportHeight > 0,
    `${viewportHeight}px`,
    "100dvh",
  );
  const topChromeHeight =
    "var(--mobile-top-chrome-height, calc(var(--mobile-topbar-h) + 52px + var(--top-banner-stack-height, 0px) + env(safe-area-inset-top, 0px)))";
  const keyboardMaxHeight =
    `calc(${visibleViewportHeight} - ${topChromeHeight} - var(--overlay-gap) - var(--overlay-gap))`;
  const tabBarMaxHeight =
    `calc(${visibleViewportHeight} - ${topChromeHeight} - var(--mobile-tabbar-height) - var(--overlay-gap) - var(--overlay-gap))`;
  return {
    keyboardOffset,
    expandedMaxHeight: selectValue(
      Boolean(keyboardOffset),
      keyboardMaxHeight,
      tabBarMaxHeight,
    ),
  };
}

export function canSubmitMobileComposer(input: {
  isSending: boolean;
  isEnhancing: boolean;
  promptTooLong: boolean;
  text: string;
  attachmentCount: number;
}): boolean {
  if (input.isSending || input.isEnhancing || input.promptTooLong) return false;
  return Boolean(input.text.trim()) || input.attachmentCount > 0;
}

export function promptCounterColor(
  promptTooLong: boolean,
  shouldShowCount: boolean,
  textLength: number,
): string {
  if (promptTooLong) return "text-[var(--danger)]";
  if (shouldShowCount || textLength > 500) return "text-[var(--amber-400)]";
  return "text-[var(--fg-3)]";
}

export function promptCounterText(
  shouldShowCount: boolean,
  textLength: number,
): string | number {
  return shouldShowCount
    ? `${textLength}/${MAX_PROMPT_CHARS}`
    : textLength;
}
