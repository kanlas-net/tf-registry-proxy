# tf-registry-proxy — caching proxy for the Terraform module registry protocol.
# No third-party runtime dependencies: the image only needs a Python interpreter.
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TFP_HOST=0.0.0.0 \
    TFP_PORT=8080 \
    TFP_CACHE_DIR=/var/cache/tf-registry-proxy

RUN groupadd --system tfproxy \
    && useradd --system --gid tfproxy --home-dir /var/cache/tf-registry-proxy --shell /usr/sbin/nologin tfproxy

WORKDIR /app
COPY tfproxy/ /app/tfproxy/

RUN mkdir -p "$TFP_CACHE_DIR" && chown -R tfproxy:tfproxy /app "$TFP_CACHE_DIR"

USER tfproxy
EXPOSE 8080

# The container is healthy when /healthz answers 200 (any error makes python exit 1).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["/usr/local/bin/python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"]

ENTRYPOINT ["/usr/local/bin/python", "-m", "tfproxy", "serve"]

# --------------------------------------------------------------------------
# Test stage: docker build --target test -t tfproxy-test . && docker run --rm tfproxy-test
# --------------------------------------------------------------------------
FROM python:3.12-slim AS test

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
COPY . /src
CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]
