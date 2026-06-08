ARG PYTHON_BASE=python:3.11-slim-bookworm

FROM ${PYTHON_BASE} AS runtime

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    MCP_APP_ROOT=/app \
    MCP_DATA_ROOT=/data \
    MCP_REGISTRY_PATH=/app/registry.yaml \
    MCP_USER_REGISTRY_PATH=/data/mcp_registry.yaml \
    MCP_TOOL_BIN_ROOT=/data/mcp_tools/bin \
    MCP_TOOL_JAVA_ROOT=/data/mcp_tools/java \
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

RUN uv pip install --system --no-cache \
        fastmcp \
        pyyaml \
        pandas \
        tabulate \
        pypdf

WORKDIR /app

RUN mkdir -p /app/storage /data/mcp_outputs

COPY server.py registry.yaml /app/

VOLUME ["/data"]

EXPOSE 8000

ENTRYPOINT ["python", "-u", "/app/server.py"]
