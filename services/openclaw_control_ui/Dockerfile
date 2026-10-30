ARG OPENCLAW_BASE_IMAGE=ghcr.io/openclaw/openclaw@sha256:5139eb3ca71ba20d3cd3aa933338e63dbff857d384e482fbdb7dd557849e5529
FROM ${OPENCLAW_BASE_IMAGE}

ARG OPENCLAW_BASE_IMAGE=ghcr.io/openclaw/openclaw@sha256:5139eb3ca71ba20d3cd3aa933338e63dbff857d384e482fbdb7dd557849e5529
ARG OPENCLAW_VERSION=2026.9.8
ARG OPENCLAW_UPSTREAM_REVISION=fc23bc864e4553c2d215e479eeec47b67a0bf943
ARG OPENCLAW_PATCH_SHA256=ec3ed1dd646d2c3e4f2c6092e308f18c4200972df35d4f9928b794487a3b25d1
ARG OPENCLAW_CONTROL_UI_BUILD_ID=2026.9.8-scm-ec3ed1dd646d2c3e
ARG OPENCLAW_BUILD_TIMESTAMP=2026-10-30T20:13:38.041Z

COPY dist/control-ui/ /app/dist/control-ui/
COPY dist/build-info.json /app/dist/build-info.json

LABEL org.opencontainers.image.version="${OPENCLAW_VERSION}" \
      org.opencontainers.image.revision="${OPENCLAW_UPSTREAM_REVISION}" \
      io.envaperu.openclaw.base-image="${OPENCLAW_BASE_IMAGE}" \
      io.envaperu.openclaw.patch.sha256="${OPENCLAW_PATCH_SHA256}" \
      io.envaperu.openclaw.control-ui.build-id="${OPENCLAW_CONTROL_UI_BUILD_ID}" \
      io.envaperu.openclaw.control-ui.built-at="${OPENCLAW_BUILD_TIMESTAMP}"
