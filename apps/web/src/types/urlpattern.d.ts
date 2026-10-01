// Type-only bridge for Next's web declarations with TypeScript 5.9 / Node 20
// ambient types. This does not install a runtime polyfill or change globals.
import "urlpattern-polyfill";

declare global {
  type URLPatternInput = string | URLPatternInit;
  interface URLPatternOptions {
    ignoreCase?: boolean;
  }
}
