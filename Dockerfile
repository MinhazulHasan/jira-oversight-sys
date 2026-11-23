#
# The Jira Oversight service: the API, and from the same image the sync service and the admin commands
# (deploy/docker-compose.yml; docs/DEPLOYMENT.md says how to run it). Build from this folder, for example:
#   docker compose -f deploy/docker-compose.yml build
#
# Python 3.13 slim. The dependencies are installed into a virtual environment in a separate stage, so no pip cache
# or build tool reaches the final image. The code is owned by root and read-only to the service, which runs as an
# unprivileged user; it writes only to /app/data (the snapshot and its backups, a volume) and /app/reports.
# Nothing here is secret: every setting comes from the environment at run time (env_file in the compose file). The
# build context is an allowlist (.dockerignore): no .env file and no data ever reaches it. The base image is the only
# pull: Docker's built-in Dockerfile frontend reads this file, so no syntax image is fetched either.

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
# The service's user and group. On a Linux host where the admin edits config/ through a bind mount, building with
# their own ids (OVERSIGHT_UID and OVERSIGHT_GID in the compose file) lets `doctor` rewrite config/fields.yaml.
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
