# syntax=docker/dockerfile:1
# ============================================================================
# SecFi Prime Platform — production API container.
#
# Build:  docker build -f infra/docker/Dockerfile -t secfi-prime-platform:latest .
# Run:    docker run -p 8000:8000 --env-file .env secfi-prime-platform:latest
#
# Multi-stage build: builder stage installs deps into a venv; runtime stage
# copies only the venv + source, runs as a non-root user, no compiler
# toolchain in the final image (smaller attack surface, required by most
# bank container security baselines).
# ============================================================================

FROM python:3.11-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir ".[db]"

FROM python:3.11-slim AS runtime

RUN groupadd --gid 1000 secfi && \
    useradd --uid 1000 --gid secfi --shell /bin/bash --create-home secfi

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=secfi:secfi src/ ./src/
COPY --chown=secfi:secfi configs/ ./configs/
COPY --chown=secfi:secfi sql/ ./sql/

ENV PYTHONPATH=/app/src

USER secfi
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

CMD ["uvicorn", "secfi_platform.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
