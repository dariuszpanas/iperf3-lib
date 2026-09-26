# Build the repository's native test image first; this adds only the example
# and the current source tree, avoiding a second native toolchain build.
ARG BASE_IMAGE=iperf3-lib-test:local
FROM ${BASE_IMAGE}

COPY src /app/src
COPY examples/observability/benchmark.py /app/examples/observability/benchmark.py

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "/app/examples/observability/benchmark.py"]
CMD ["hold"]
