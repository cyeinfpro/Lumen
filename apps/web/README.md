# Lumen Web

Lumen Web is the Next.js frontend for the Lumen workspace. It renders Studio, video generation, assets, projects, settings, account screens, admin panels, share pages and global task state.

## Runtime Model

- Browser API calls default to same-origin `/api`.
- `src/proxy.ts` rewrites `/api/*` and `/events` to FastAPI through `LUMEN_BACKEND_URL`.
- Docker runs the standalone Next.js server on port `3000`.
- Production should normally expose Web through a reverse proxy; direct public `:3000` exposure is opt-in.

## Environment

| Variable | Purpose |
| --- | --- |
| `LUMEN_BACKEND_URL` | Server-side FastAPI origin used by `src/proxy.ts`. Default local value is `http://127.0.0.1:8000`; Docker compose uses `http://api:8000`. |
| `NEXT_PUBLIC_API_BASE` | Browser-visible API base. Use `/api` for same-origin deployments. |
| `NEXT_PUBLIC_LUMEN_VERSION` | Displayed product version. Accepts `1.2.3` or `v1.2.3`. |
| `NEXT_PUBLIC_SENTRY_DSN` / `SENTRY_DSN` | Optional Sentry client/server DSN. |
| `NEXT_PUBLIC_SENTRY_ENV` / `SENTRY_ENV` | Optional Sentry environment. |
| `LUMEN_UPGRADE_INSECURE_REQUESTS` | Enables production CSP `upgrade-insecure-requests` only when explicitly set to `true`. |
| `LUMEN_HSTS_INCLUDE_SUBDOMAINS` | Adds `includeSubDomains` to HSTS only when explicitly set to `true`. |

## Development

```bash
cd apps/web
npm ci
npm run dev
```

Start the API separately on `127.0.0.1:8000`, or set `LUMEN_BACKEND_URL` to the target backend.

## Commands

```bash
npm run dev
npm run build
npm run start
npm test
npm run lint
npm run lint:eslint
npm run check:ui-governance
npm run check:ui-governance:update
npm run type-check
npm run type-check:full
```

## UI Rules

- Use the shared shell/navigation primitives under `src/components/ui/shell`.
- Keep theme colors on semantic tokens from `src/app/globals.css`.
- Do not add hard-coded dark UI outside media/lightbox/code/scrim/danger surfaces.
- Mobile dialogs and sheets should use existing mobile primitives or the safe-area utilities defined in globals.
- `npm run lint` includes the UI governance scanner; update its baseline only for intentional, reviewed exceptions.
