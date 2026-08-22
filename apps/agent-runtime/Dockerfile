ARG NODE_BASE=node:22.22.0-alpine

FROM ${NODE_BASE} AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts

FROM dependencies AS build
COPY tsconfig.json tsconfig.build.json eslint.config.js ./
COPY src ./src
RUN npm run build && npm prune --omit=dev --ignore-scripts

FROM ${NODE_BASE} AS runtime
ARG LUMEN_VERSION=0.0.0
ARG LUMEN_GIT_SHA=unknown
ARG LUMEN_BUILD_TIME=1970-01-01T00:00:00Z
LABEL org.opencontainers.image.source="https://github.com/cyeinfpro/Lumen" \
      org.opencontainers.image.version="${LUMEN_VERSION}" \
      org.opencontainers.image.revision="${LUMEN_GIT_SHA}" \
      org.opencontainers.image.created="${LUMEN_BUILD_TIME}"
ENV NODE_ENV=production \
    PI_OFFLINE=1 \
    PI_SKIP_VERSION_CHECK=1 \
    PI_TELEMETRY=0
WORKDIR /app
COPY --from=build --chown=node:node /app/package.json /app/package-lock.json ./
COPY --from=build --chown=node:node /app/node_modules ./node_modules
COPY --from=build --chown=node:node /app/dist ./dist
USER node
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:8090/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "dist/server.js"]
