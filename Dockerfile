ARG PYTHON_BASE=python:3.11-slim-bookworm

FROM ${PYTHON_BASE} AS builder

ENV PIP_NO_CACHE_DIR=1

RUN python -m pip install --upgrade pip \
    && pip wheel --no-cache-dir --retries 5 --timeout 120 --wheel-dir /wheels \
        fastmcp \
        pyyaml \
        pandas \
        tabulate \
        pypdf

FROM ${PYTHON_BASE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_APP_ROOT=/app \
    MCP_DATA_ROOT=/data \
    MCP_REGISTRY_PATH=/app/registry.yaml \
    MCP_USER_REGISTRY_PATH=/data/mcp_registry.yaml \
    MCP_TOOL_BIN_ROOT=/data/mcp_tools/bin \
    MCP_TRANSPORT=stdio \
    MCP_HOST=127.0.0.1 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp

RUN apt-get update \
    -o Acquire::Retries=5 \
    && apt-get install -y --no-install-recommends \
        -o Acquire::Retries=5 \
        bedtools \
        ca-certificates \
        curl \
        default-jre-headless \
        fastp \
        ncbi-blast+ \
        r-base \
        samtools \
        wget \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /var/cache/apt/*.bin

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* \
    && rm -rf /wheels

WORKDIR /app

RUN mkdir -p /app/storage /data/mcp_outputs

COPY server.py registry.yaml /app/

VOLUME ["/data"]

EXPOSE 8000

ENTRYPOINT ["python", "-u", "/app/server.py"]
