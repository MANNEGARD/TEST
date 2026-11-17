# SMGD Transkribering – en container: Node-server, webbapp och ffmpeg.
FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json ./
COPY src ./src
RUN npm run build && npm prune --omit=dev

FROM node:22-bookworm-slim
LABEL org.opencontainers.image.source="https://github.com/SMGD-MP/TEST" \
      org.opencontainers.image.description="SMGD Transkribering – svensk transkribering med talaruppdelning"
RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg tini \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV NODE_ENV=production \
    TRANSCRIBE_DATA_DIR=/data \
    TRANSCRIBE_WEB_DIR=/app/web \
    TRANSCRIBE_PORT=8080 \
    TZ=Europe/Stockholm
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY package.json ./
COPY web ./web
RUN mkdir -p /data && chown node:node /data
USER node
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:8080/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "--max-old-space-size=256", "dist/server.js"]
