/** Strip queries, fragments and secret route segments before telemetry. */
export function monitoringRoute(href: string, base: string): string {
  const path = new URL(href, base).pathname;
  return path.replace(/^(\/(?:reset-password|invite|share))\/[^/]+/, "$1/[token]");
}
