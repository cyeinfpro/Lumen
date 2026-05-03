# Lumen Web (Next.js) - multi-stage build, runs in standalone mode (node server.js)

# ---- deps: install npm dependencies (with native build tools, then strip) ----
FROM node:22-alpine AS deps
WORKDIR /app
RUN apk add --no-cache --virtual .build-deps libc6-compat python3 make g++
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund --prefer-offline \
    && apk del .build-deps

# ---- builder: compile Next.js to .next/standalone ----
FROM node:22-alpine AS builder
WORKDIR /app
ARG LUMEN_VERSION=dev
ARG LUMEN_BACKEND_URL=http://api:8000
ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_API_BASE=/api \
    LUMEN_BACKEND_URL=${LUMEN_BACKEND_URL}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---- runner: minimal runtime image ----
FROM node:22-alpine AS runner
WORKDIR /app
RUN apk add --no-cache wget dumb-init \
    && addgroup -g 10001 nodejs \
    && adduser -S -D -H -u 10001 -G nodejs nextjs
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
EXPOSE 3000
USER nextjs
ARG LUMEN_VERSION=dev
ARG LUMEN_REVISION=unknown
ARG LUMEN_CREATED=unknown
LABEL org.opencontainers.image.source="https://github.com/cyeinfpro/Lumen" \
      org.opencontainers.image.title="lumen-web" \
      org.opencontainers.image.description="Lumen Next.js frontend (standalone)" \
      org.opencontainers.image.version="${LUMEN_VERSION}" \
      org.opencontainers.image.revision="${LUMEN_REVISION}" \
      org.opencontainers.image.created="${LUMEN_CREATED}"
ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "server.js"]
