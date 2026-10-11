# syntax=docker/dockerfile:1
#
# MCP server for the MiniReachy (Reachy Mini) daemon.
# The daemon itself is NOT installed here: it keeps running natively on the host.
#
# Targets:
#   runtime (default) - the MCP server, non-root, read-only friendly
#   test              - runtime + test dependencies + test suite (no robot needed)

ARG PYTHON_IMAGE=python:3.12.15-slim-trixie

# ---------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.lock /tmp/requirements.lock
RUN pip install --require-hashes --only-binary=:all: -r /tmp/requirements.lock

FROM deps AS test-deps
COPY requirements-test.lock /tmp/requirements-test.lock
RUN pip install --require-hashes --only-binary=:all: -r /tmp/requirements-test.lock

# ---------------------------------------------------------------------------------
# Local speech models (faster-whisper + Piper French and English voices), pinned revisions.
# Downloaded at build time only: the runtime container never fetches models.
FROM deps AS models
ARG WHISPER_REPO=Systran/faster-whisper-small
ARG WHISPER_REVISION=536b0662742c02347bc0e980a01041f333bce120
ARG PIPER_VOICES=fr/fr_FR/siwis/medium/fr_FR-siwis-medium,en/en_US/lessac/medium/en_US-lessac-medium
ARG PIPER_REVISION=c10ece1aade47bb51c153c893d14e5bf8e5b7117
COPY scripts/download_models.py /tmp/download_models.py
RUN python /tmp/download_models.py "$WHISPER_REPO" "$WHISPER_REVISION" "$PIPER_VOICES" "$PIPER_REVISION" /opt/models

# ---------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/opt/venv/bin:$PATH
RUN groupadd --system --gid 10001 mcp \
 && useradd --system --uid 10001 --gid mcp --no-create-home --shell /usr/sbin/nologin mcp
WORKDIR /app

# ---------------------------------------------------------------------------------
FROM base AS test
COPY --from=test-deps /opt/venv /opt/venv
COPY --from=models /opt/models /opt/models
COPY pyproject.toml compose.yaml Dockerfile ./
COPY src ./src
COPY tests ./tests
COPY docs/api/openapi.json ./docs/api/openapi.json
USER mcp
ENV PYTEST_ADDOPTS="-p no:cacheprovider"
CMD ["python", "-m", "pytest", "-m", "not e2e", "-q"]

# ---------------------------------------------------------------------------------
FROM base AS runtime
COPY --from=deps /opt/venv /opt/venv
COPY --from=models /opt/models /opt/models
COPY src ./src
USER mcp
EXPOSE 8765
# Liveness of the MCP process only (does not depend on the robot daemon).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('MCP_PORT','8765'), timeout=4).status == 200 else 1)"]
# exec form: python is PID 1's child under `init: true`, receives SIGTERM directly.
CMD ["python", "-m", "minireachy_mcp"]
