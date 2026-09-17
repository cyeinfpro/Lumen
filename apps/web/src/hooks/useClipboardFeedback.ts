"use client";

import { useEffect, useRef, useState } from "react";
import { tryCopyTextToClipboard } from "@/lib/clipboard";

type CopyAction = () => boolean | void | Promise<boolean | void>;

/** A success checkmark means copying completed, not merely that a click occurred. */
export function useClipboardFeedback() {
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [errorKey, setErrorKey] = useState<string | null>(null);
  const [pendingKey, setPendingKey] = useState<string | null>(null);
  const timer = useRef<number | null>(null);
  const busy = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, []);

  const copy = async (key: string, text: string, action?: CopyAction) => {
    if (busy.current) return false;
    busy.current = true;
    if (timer.current !== null) window.clearTimeout(timer.current);
    setCopiedKey(null);
    setErrorKey(null);
    setPendingKey(key);
    let succeeded: boolean | void = false;
    try {
      succeeded = action ? await action() : await tryCopyTextToClipboard(text);
    } catch {
      succeeded = false;
    } finally {
      busy.current = false;
    }
    if (!mounted.current) return succeeded === true;
    setPendingKey(null);
    // Legacy callbacks own their asynchronous notices. A void return confirms
    // neither success nor failure, so do not invent either local result.
    if (succeeded === undefined) return false;
    if (!succeeded) {
      setErrorKey(key);
      return false;
    }
    setCopiedKey(key);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      setCopiedKey((current) => current === key ? null : current);
    }, 1400);
    return true;
  };

  return { copiedKey, errorKey, pendingKey, copy };
}
