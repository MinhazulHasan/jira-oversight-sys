# The Jira Oversight service image: the API, the sync service and the admin commands.
ARG PYTHON_IMAGE=python:3.13-slim

# --- dependencies ----------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
# The test runner is not shipped.
RUN grep -v '^pytest==' requirements.txt > runtime-requirements.txt \
 && python -m venv /opt/venv \
 && /opt/venv/bin/pip install -r runtime-requirements.txt

# --- the service -------------------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
ARG APP_UID=10001
ARG APP_GID=10001
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    API_HOST=0.0.0.0 \
    API_PORT=3300 \
    LOG_FORMAT=json
RUN groupadd --system --gid "${APP_GID}" oversight \
 && useradd --system --uid "${APP_UID}" --gid oversight --home-dir /app --no-create-home \
      --shell /usr/sbin/nologin oversight
WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY oversight ./oversight
COPY config ./config
COPY run.py ./
# Compiled once here, as root, so no process ever needs to write bytecode; then the two folders the service writes.
RUN python -m compileall -q oversight \
 && mkdir -p data reports \
 && chown oversight:oversight data reports
USER oversight
VOLUME ["/app/data"]
EXPOSE 3300
# GET /api/v1/health with the service key, on this container's own port (oversight/healthcheck.py).
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "-m", "oversight.healthcheck"]
CMD ["python", "-m", "oversight", "api"]
