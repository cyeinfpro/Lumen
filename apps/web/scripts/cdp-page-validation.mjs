export const PAGE_IDENTITY_EXPRESSION = String.raw`(() => {
  const body = document.body;
  const bodyText = (body?.innerText ?? "").trim();
  const meaningfulSelector =
    "main, form, button, a[href], input, textarea, select, img, svg, canvas, video, [role]";
  let meaningfulElementCount = 0;
  for (const element of body?.querySelectorAll(meaningfulSelector) ?? []) {
    if (!(element instanceof HTMLElement || element instanceof SVGElement)) {
      continue;
    }
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    if (
      style.display !== "none" &&
      style.visibility !== "hidden" &&
      Number(style.opacity) >= 0.02 &&
      rect.width >= 1 &&
      rect.height >= 1
    ) {
      meaningfulElementCount += 1;
      if (meaningfulElementCount >= 4) break;
    }
  }
  const navigation = performance.getEntriesByType("navigation").at(-1);
  const responseStatus =
    typeof navigation?.responseStatus === "number"
      ? navigation.responseStatus
      : null;
  const redirectCount =
    typeof navigation?.redirectCount === "number"
      ? navigation.redirectCount
      : 0;
  const nextPortal = document.querySelector("nextjs-portal");
  const portalRoot = nextPortal?.shadowRoot ?? null;
  const portalText = (portalRoot?.textContent ?? "").slice(0, 2000);
  const errorText = bodyText.slice(0, 2000) + "\n" + portalText;
  const errorMarker =
    Boolean(
      document.querySelector("[data-nextjs-dialog-overlay]"),
    ) ||
    Boolean(
      portalRoot?.querySelector(
        '[data-nextjs-dialog-overlay], [data-nextjs-error-overlay], [role="dialog"]',
      ),
    ) ||
    /Application error: a client-side exception has occurred|Internal Server Error|Unhandled Runtime Error/i.test(
      errorText,
    );
  return {
    finalUrl: location.href,
    pathname: location.pathname,
    title: document.title,
    responseStatus,
    redirectCount,
    bodyTextLength: bodyText.length,
    bodyChildCount: body?.childElementCount ?? 0,
    meaningfulElementCount,
    errorMarker,
  };
})()`;

function combinedSignal(signal, timeoutSignal) {
  if (!signal) return timeoutSignal;
  if (typeof AbortSignal.any === "function") {
    return AbortSignal.any([signal, timeoutSignal]);
  }
  const controller = new AbortController();
  const abort = (source) => {
    if (!controller.signal.aborted) controller.abort(source.reason);
  };
  signal.addEventListener("abort", () => abort(signal), { once: true });
  timeoutSignal.addEventListener(
    "abort",
    () => abort(timeoutSignal),
    { once: true },
  );
  return controller.signal;
}

export async function fetchJsonWithTimeout(
  url,
  init = {},
  {
    timeoutMs = 5_000,
    fetchImpl = globalThis.fetch,
  } = {},
) {
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  const signal = combinedSignal(init.signal, timeoutSignal);
  let response;
  try {
    response = await fetchImpl(url, { ...init, signal });
  } catch (error) {
    if (timeoutSignal.aborted) {
      throw new Error(
        `HTTP request timed out after ${timeoutMs}ms: ${url}`,
        { cause: error },
      );
    }
    throw error;
  }
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}: ${url}`);
  }
  return response.json();
}

function comparableUrl(value) {
  const url = new URL(value);
  return `${url.origin}${url.pathname}${url.search}`;
}

export function pageIdentityErrors(
  requestedUrl,
  identity,
  { expectedStatuses = [200] } = {},
) {
  const errors = [];
  if (!identity || typeof identity !== "object") {
    return ["page identity inspection returned no data"];
  }

  try {
    const expected = comparableUrl(requestedUrl);
    const actual = comparableUrl(identity.finalUrl);
    if (actual !== expected) {
      errors.push(`wrong final URL: expected ${expected}, received ${actual}`);
    }
  } catch {
    errors.push(`invalid final URL: ${String(identity.finalUrl)}`);
  }

  if (identity.redirectCount !== 0) {
    errors.push(`unexpected redirect count: ${identity.redirectCount}`);
  }
  if (
    !Number.isInteger(identity.responseStatus) ||
    !expectedStatuses.includes(identity.responseStatus)
  ) {
    errors.push(
      `unexpected document status: ${String(identity.responseStatus)} (expected ${expectedStatuses.join("/")})`,
    );
  }
  if (
    identity.bodyTextLength === 0 &&
    identity.meaningfulElementCount === 0
  ) {
    errors.push("blank page: no visible text or meaningful elements");
  }
  if (identity.errorMarker) {
    errors.push("framework/runtime error page marker detected");
  }
  return errors;
}
