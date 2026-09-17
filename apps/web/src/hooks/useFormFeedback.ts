"use client";

import { useEffect, useState } from "react";

interface FormIssue {
  message: string;
  fieldId?: string;
}

/** Keep feedback next to its form and move focus only after controls re-enable. */
export function useFormFeedback(errorId: string) {
  const [issue, setIssue] = useState<FormIssue | null>(null);

  useEffect(() => {
    if (!issue) return;
    const target = document.getElementById(issue.fieldId ?? errorId);
    if (!target || target.matches(":disabled")) return;
    target.focus({ preventScroll: true });
    target.scrollIntoView({ block: "nearest", behavior: "instant" });
  }, [errorId, issue]);

  const fieldProps = (fieldId: string, descriptionId?: string) => {
    const invalid = issue?.fieldId === fieldId;
    return {
      "aria-invalid": invalid || undefined,
      "aria-describedby": [descriptionId, invalid ? errorId : null]
        .filter(Boolean).join(" ") || undefined,
    };
  };

  return {
    errorId,
    message: issue?.message ?? null,
    fieldProps,
    report: (message: string, fieldId?: string) => setIssue({ message, fieldId }),
    clear: () => setIssue(null),
  };
}
