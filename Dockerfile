# RICH in a container: the wheel, Bubblewrap, and the exact toolchain it pins.
#
#   docker build -t rich .
#   docker run --rm -p 127.0.0.1:8767:8767 -v rich-state:/rich -e ANTHROPIC_API_KEY \
#     --security-opt seccomp=docker/seccomp.json --security-opt apparmor=unconfined rich
#
# The port is published only to the host's loopback; inside, the server binds
# 0.0.0.0 with its Host and Origin checks enforced. Building software needs
# unprivileged user namespaces for Bubblewrap, which Docker's default seccomp
# profile blocks; docker/seccomp.json is that profile with the namespace and
# mount syscalls admitted. `rich doctor` inside the container says whether
# they took effect.

ARG NODE_VERSION=22.23.2
ARG PNPM_VERSION=10.34.5

# ---- toolchain: what every stage shares -----------------------------------
FROM ubuntu:24.04 AS toolchain
ARG NODE_VERSION
ARG PNPM_VERSION
ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive \
    COREPACK_HOME=/opt/corepack \
    PATH=/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN apt-get update && apt-get install -y --no-install-recommends \
      bubblewrap git curl ca-certificates xz-utils \
      python3 python3-venv python3-pip \
      # Chromium's system libraries: Playwright downloads the browser into
      # the dependency cache on the state volume, never into the image.
      libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
      libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libasound2t64 libpango-1.0-0 libcairo2 libatspi2.0-0t64 \
      libx11-6 libx11-xcb1 libxcb1 libxext6 libglib2.0-0t64 libdbus-1-3 \
      libxshmfence1 fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*
# Node: the exact version the executor checks, verified against the checksums
# nodejs.org publishes for it, installed as one root so `which node` resolves
# to <root>/bin/node with <root>/include/node/node_version.h beside it.
RUN set -eu; \
    case "${TARGETARCH}" in amd64) arch=x64 ;; arm64) arch=arm64 ;; *) echo "unsupported arch ${TARGETARCH}"; exit 1 ;; esac; \
    tarball="node-v${NODE_VERSION}-linux-${arch}.tar.xz"; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/${tarball}"; \
    curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"; \
    grep " ${tarball}\$" SHASUMS256.txt | sha256sum -c -; \
    mkdir -p /opt/node && tar -xJf "${tarball}" -C /opt/node --strip-components=1; \
    rm -f "${tarball}" SHASUMS256.txt; \
    test -f /opt/node/include/node/node_version.h; \
    /opt/node/bin/node --version | grep -qx "v${NODE_VERSION}"
# pnpm: prepared into the Corepack cache the executor reads
# (COREPACK_HOME/v1/pnpm/<version>/bin/pnpm.cjs), and a shim on PATH for people.
RUN set -eu; \
    /opt/node/bin/corepack enable --install-directory /usr/local/bin; \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 /opt/node/bin/corepack prepare "pnpm@${PNPM_VERSION}" --activate; \
    test -f "/opt/corepack/v1/pnpm/${PNPM_VERSION}/bin/pnpm.cjs"; \
    chmod -R a+rX /opt/corepack

# ---- builder: the canvas and the wheel --------------------------------------
FROM toolchain AS builder
WORKDIR /src
COPY . .
RUN npm --prefix web ci && python3 tools/build_wheel.py --skip-install

# ---- runtime -----------------------------------------------------------------
FROM toolchain
RUN useradd --create-home --uid 1001 rich \
    && python3 -m venv /opt/rich \
    && mkdir -p /rich && chown rich:rich /rich
COPY --from=builder /src/dist/rich_agent_build_system-*.whl /tmp/
RUN /opt/rich/bin/pip install --no-cache-dir /tmp/rich_agent_build_system-*.whl \
    && rm -f /tmp/rich_agent_build_system-*.whl \
    && ln -s /opt/rich/bin/rich /usr/local/bin/rich
USER rich
ENV HOME=/home/rich
VOLUME ["/rich"]
EXPOSE 8767
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -sf http://127.0.0.1:8767/v1/health || exit 1
CMD ["rich", "serve", "--state-dir", "/rich/state", "--host", "0.0.0.0", "--published-on-loopback", "--route", "api"]
