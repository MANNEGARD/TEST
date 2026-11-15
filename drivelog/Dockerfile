# syntax=docker/dockerfile:1
#
# SMGD DriveLog som Home Assistant-tillägg: API, webbapp och Caddy i EN container.
#
# Byggs av GitHub Actions med REPOTS ROT som byggkontext (Supervisor hämtar bara den färdiga
# avbilden, se drivelog/config.yaml):
#
#   docker buildx build -f drivelog/Dockerfile --platform linux/amd64,linux/arm64 .
#
# Arkitekturer: webbappen byggs EN gång på byggmaskinens egen arkitektur ($BUILDPLATFORM) –
# Next-bygget är tungt men ger arkitekturoberoende JavaScript. Det enda arkitekturberoende i
# Nexts standalone-utdata är bildoptimeraren sharp (@img/sharp-*, inbyggda .node- och .so-filer).
# Webbappen använder inte next/image, så sharp tas bort och bygget KONTROLLERAR att inga inbyggda
# binärer finns kvar. Node, Python-paketen och Caddy kommer däremot från avbilder för mål-
# arkitekturen, så bara pip-installationen körs under emulering för arm64.

# Bas-avbilderna är fästa vid digest (index för alla arkitekturer, uppslaget 2026-11-15): en flyttad
# tagg uppströms hamnar aldrig oprövad i en publicerad avbild. Dependabot (.github/dependabot.yml)
# föreslår nya digest som vanliga ändringar som går igenom CI. Samma avbild skrivs ut på varje
# FROM-rad (inte via ARG) så att Dependabot hittar och uppdaterar alla.

# --- Webbappen (Next.js standalone), byggs en gång för alla arkitekturer --------------------------
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS webb
WORKDIR /src
ENV NEXT_TELEMETRY_DISABLED=1 \
    API_INTERNAL_URL=http://127.0.0.1:8000
COPY frontend/package.json frontend/package-lock.json ./
COPY frontend/scripts ./scripts
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN node scripts/copy-maplibre-worker.mjs && npm run build
RUN set -eu; \
    if grep -rqE "from ['\"]next/image['\"]" src; then \
      echo "Webbappen använder next/image – då behövs sharp för varje arkitektur. Se drivelog/Dockerfile." >&2; \
      exit 1; \
    fi; \
    rm -rf .next/standalone/node_modules/sharp .next/standalone/node_modules/@img; \
    binarer="$(find .next/standalone \( -name '*.node' -o -name '*.so' -o -name '*.so.*' \) -print)"; \
    if [ -n "$binarer" ]; then \
      echo "Arkitekturberoende filer i webbappen – avbilden skulle bara fungera på $(uname -m):" >&2; \
      echo "$binarer" >&2; \
      exit 1; \
    fi; \
    mkdir -p /ut/.next; \
    cp -a .next/standalone/. /ut/; \
    cp -a .next/static /ut/.next/static; \
    cp -a public /ut/public

# --- Backendens Python-paket, för målarkitekturen ----------------------------------------------
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS api
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY backend/pyproject.toml /src/backend/pyproject.toml
# Som i backend/Dockerfile: bara beroendena installeras här; koden kopieras till /app nedan.
RUN /opt/venv/bin/pip install /src/backend

# --- Officiella binärer för målarkitekturen (kopieras, körs inte här) --------------------------
FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS node
FROM caddy:2.10-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d AS caddy

# --- Slutlig avbild ------------------------------------------------------------------------------
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS bas
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
# libstdc++6 behövs av node; setpriv (util-linux) finns redan i Debian och används för att köra
# processerna som icke-root.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --no-create-home --home-dir /data/app --shell /usr/sbin/nologin --uid 10001 drivelog \
    && useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --uid 10002 drivelog-web \
    && useradd --system --no-create-home --home-dir /data/caddy --shell /usr/sbin/nologin --uid 10003 caddy
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=caddy /usr/bin/caddy /usr/local/bin/caddy
COPY --from=api /opt/venv /opt/venv
WORKDIR /app
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY --from=webb /ut /opt/drivelog/web
COPY drivelog/rootfs/ /
ARG BUILD_VERSION=dev
ENV DRIVELOG_ADDON_VERSION=${BUILD_VERSION}
RUN chmod 0755 /usr/local/bin/drivelog-addon /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /opt/drivelog/web/.next/cache \
    && chown drivelog-web:drivelog-web /opt/drivelog/web/.next/cache \
    # Röktest av avbilden (körs under emulering för arm64): alla tre körtidsmiljöerna startar.
    && node --version \
    && caddy version \
    && python -c "import sys; assert sys.version_info[:2] == (3, 13), sys.version" \
    && DRIVELOG_ENVIRONMENT=test DRIVELOG_DATABASE_URL=sqlite:// python -c "import app.main" \
    && python -I -c "import sys; sys.path.insert(0, '/opt/drivelog-addon'); import drivelog_addon.startprogram"
EXPOSE 80 443 443/udp
CMD ["/usr/local/bin/drivelog-addon"]

# Etiketter som Supervisor läser (io.hass.*) – arkitekturen i Home Assistants namn.
FROM bas AS slut-amd64
ARG BUILD_VERSION=dev
LABEL io.hass.arch="amd64"
FROM bas AS slut-arm64
ARG BUILD_VERSION=dev
LABEL io.hass.arch="aarch64"

FROM slut-${TARGETARCH}
ARG BUILD_VERSION=dev
LABEL io.hass.type="addon" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.name="SMGD DriveLog" \
      io.hass.description="Självhostad körjournal för tjänsteresor" \
      org.opencontainers.image.title="SMGD DriveLog (Home Assistant-tillägg)" \
      org.opencontainers.image.source="https://github.com/SMGD-MP/TEST" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.licenses="NOASSERTION"
