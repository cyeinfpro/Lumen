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
    void refetchMessages({ cancelRefetch: false }).finally(() => {
      checkingRef.current = false;
      setCheckingSubmission(false);
    });
    reconnect();
  }, [reconnect, refetchMessages]);

  return { checkingSubmission, retryMessages };
}
