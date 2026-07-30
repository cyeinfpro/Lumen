const VIDEO_DIALOG_SELECTOR = '[role="dialog"][aria-modal="true"]';
const VIDEO_DIALOG_FOCUSABLE =
  'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';

function openVideoDialogs(): HTMLElement[] {
  if (typeof document === "undefined") return [];
  return Array.from(
    document.querySelectorAll<HTMLElement>(VIDEO_DIALOG_SELECTOR),
  ).filter((dialog) => dialog.isConnected);
}

export function isTopmostDialog(dialog: HTMLElement | null): boolean {
  if (!dialog?.isConnected) return false;
  const dialogs = openVideoDialogs();
  return dialogs[dialogs.length - 1] === dialog;
}

export function focusWorkbenchElement(
  target: HTMLElement | null,
  options?: FocusOptions,
  blocked = false,
): boolean {
  if (!target?.isConnected || blocked) return false;
  const dialogs = openVideoDialogs();
  const topmostDialog = dialogs[dialogs.length - 1];
  if (topmostDialog && !topmostDialog.contains(target)) return false;
  target.focus(options);
  return true;
}

export function restoreWorkbenchFocus(
  previousFocus: HTMLElement | null,
  closingDialog: HTMLElement | null,
  focus: (target: HTMLElement) => void = (target) =>
    target.focus({ preventScroll: true }),
): void {
  if (typeof window === "undefined") return;
  window.requestAnimationFrame(() => {
    if (!previousFocus?.isConnected) return;
    const otherDialogOpen = openVideoDialogs().some(
      (dialog) => dialog !== closingDialog,
    );
    if (otherDialogOpen) return;
    const active = document.activeElement;
    if (
      active instanceof HTMLElement &&
      active !== document.body &&
      active.isConnected &&
      !closingDialog?.contains(active)
    ) {
      return;
    }
    focus(previousFocus);
  });
}

export function trapDialogFocus(
  event: KeyboardEvent,
  dialog: HTMLElement | null,
): void {
  if (event.key !== "Tab" || !dialog) return;
  const focusable = Array.from(
    dialog.querySelectorAll<HTMLElement>(VIDEO_DIALOG_FOCUSABLE),
  ).filter((element) => element.offsetParent !== null);
  if (focusable.length === 0) {
    event.preventDefault();
    dialog.focus({ preventScroll: true });
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !dialog.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}
