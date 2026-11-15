# SMGD DriveLog – webbgränssnitt (Next.js, standalone-output)
# Bygg: docker build -t smgd-drivelog-web ./frontend

FROM node:26-alpine AS deps
WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY package.json package-lock.json ./
COPY scripts ./scripts
RUN npm ci --no-audit --no-fund

FROM node:26-alpine AS builder
WORKDIR /app
# Rewrites (/api/v1 → backend) bakas in vid byggtillfället i standalone-läge.
# Standard matchar docker-compose (tjänsten "api"). Ändra med --build-arg vid behov.
ARG API_INTERNAL_URL=http://api:8000
ENV NEXT_TELEMETRY_DISABLED=1 \
    API_INTERNAL_URL=$API_INTERNAL_URL
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN node scripts/copy-maplibre-worker.mjs && npm run build

FROM node:26-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
RUN addgroup -S -g 1001 nodejs && adduser -S -u 1001 -G nodejs nextjs
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:3000/api/healthz >/dev/null 2>&1 || exit 1
CMD ["node", "server.js"]
