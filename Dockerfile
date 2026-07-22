ARG PYTHON_BASE=python:3.12-slim

FROM ghcr.io/astral-sh/uv:0.11.31 AS uv
FROM ${PYTHON_BASE}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ARG DEBIAN_FRONTEND=noninteractive
ARG IPERF3_VERSION=3.21

# Build libiperf from an official release. libsctp-dev enables SCTP support.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libffi-dev \
        libsctp-dev \
        libssl-dev \
        pkg-config \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    cd /tmp; \
    release_url="https://github.com/esnet/iperf/releases/download/${IPERF3_VERSION}"; \
    archive="iperf-${IPERF3_VERSION}.tar.gz"; \
    curl -fsSLO "${release_url}/${archive}"; \
    curl -fsSLO "${release_url}/${archive}.sha256"; \
    sha256sum --check "${archive}.sha256"; \
    tar -xzf "${archive}"; \
    cd "iperf-${IPERF3_VERSION}"; \
    ./configure --prefix=/usr/local; \
    make -j"$(nproc)"; \
    make install; \
    ldconfig; \
    rm -rf "/tmp/iperf-${IPERF3_VERSION}" "/tmp/${archive}" "/tmp/${archive}.sha256"

COPY --from=uv /uv /uvx /usr/local/bin/

ENV VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Cache the frozen development environment independently from source changes.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --dev --no-install-project

COPY src ./src
COPY tests ./tests
RUN uv sync --frozen --dev

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["pytest", "-q"]
