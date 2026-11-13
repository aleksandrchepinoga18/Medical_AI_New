FROM node:22.21.1-bookworm-slim AS dependencies
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY frontend/package*.json ./
# До создания пользователем lockfile используется npm install; после — npm ci.
RUN if [ -f package-lock.json ]; then npm ci --include=dev; else npm install --include=dev --package-lock=false; fi \
    && test -x node_modules/.bin/next

FROM dependencies AS development
ENV NODE_ENV=development HOSTNAME=0.0.0.0 PORT=3000
COPY frontend/ ./
RUN mkdir -p .next && chown -R node:node /app
USER node
CMD ["npm", "run", "dev"]

FROM dependencies AS builder
COPY frontend/ ./
RUN mkdir -p public && npm run build && test -f .next/standalone/server.js

FROM node:22.21.1-bookworm-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public
USER node
CMD ["node", "server.js"]
