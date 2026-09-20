"use client";

import { useCallback, useRef, useState } from "react";

type RefetchMessages = (
  options: { cancelRefetch: false },
) => Promise<unknown>;

export function useAgentSubmissionReconciliation(
  refetchMessages: RefetchMessages,
  reconnect: () => void,
) {
  const [checkingSubmission, setCheckingSubmission] = useState(false);
  const checkingRef = useRef(false);
  const retryMessages = useCallback(() => {
    if (checkingRef.current) return;
    checkingRef.current = true;
    setCheckingSubmission(true);
    // Query state owns the visible error. Do not create an unhandled rejection
    // from the cleanup promise when a cancelled/refused check rejects.
    void refetchMessages({ cancelRefetch: false }).catch(() => undefined).finally(() => {
      checkingRef.current = false;
      setCheckingSubmission(false);
    });
    reconnect();
  }, [reconnect, refetchMessages]);

  return { checkingSubmission, retryMessages };
}
