ARG PYTHON_BASE=python:3.12-slim
FROM ${PYTHON_BASE}

# Build-time argument to select which iperf3 release to build
# Default to 3.20 (use ARG to override if needed)
ARG IPERF3_VERSION=3.20

# Install system dependencies: build tools, runtime libs, and packages needed to build iperf3
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        autoconf \
        automake \
        libtool \
        pkg-config \
        git \
        curl \
        ca-certificates \
        libssl-dev \
        zlib1g-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

ARG IPERF3_ENABLE_MPTCP=0
# Build and install iperf3 from source so we can select a new enough version
RUN set -eux; \
    cd /tmp; \
    # prefer official es.net downloads which publish tarballs like iperf-3.19.1.tar.gz
    curl -fsSL -o iperf.tar.gz "https://downloads.es.net/pub/iperf/iperf-${IPERF3_VERSION}.tar.gz"; \
    tar xzf iperf.tar.gz; \
    cd iperf-*; \
    # Some releases include configure, others need bootstrap; try both
    if [ -x ./bootstrap.sh ]; then ./bootstrap.sh; fi; \
    if [ -x ./configure ]; then \
        # Run the package's configure script with a standard prefix. Enabling
        # MPTCP at build time requires kernel and header support on the host
        # which may not be present inside the container; do not force it here.
        ./configure --prefix=/usr/local || true; \
    fi; \
    make -j"$(nproc)"; \
    make install; \
    ldconfig; \
    rm -rf /tmp/iperf*;

# Create app directory (code will be mounted here at runtime)
WORKDIR /app

# Create and activate a virtual environment
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Pre-install common dependencies so they're cached in the image
# (cffi, pydantic, pytest, pytest-asyncio, ty - the actual project install happens at runtime)
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir cffi pydantic pytest pytest-asyncio ty

# Entrypoint script: install project in editable mode then run command
# This allows code changes without rebuilding the image
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["pytest", "-q"]
