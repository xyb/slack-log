# slack-log — FastAPI search service + slackdump-based refresh pipeline.
# Same image serves two roles: the web Deployment (default CMD) and the
# data-refresh CronJob (command overridden to scripts/refresh.sh).

# ---- Python dependencies ----
FROM python:3.11-slim AS builder
RUN pip install --no-cache-dir --prefix=/install \
    pyyaml jinja2 emoji tqdm python-dotenv \
    fastapi "uvicorn[standard]" authlib itsdangerous httpx

# ---- runtime ----
FROM python:3.11-slim
ARG SLACKDUMP_VERSION=4.3.0
WORKDIR /app

# slackdump CLI — used by the refresh CronJob to archive the workspace.
# ca-certificates is kept (HTTPS for slackdump + attachment downloads); curl
# is only needed to fetch the binary, so it is purged afterwards.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://github.com/rusq/slackdump/releases/download/v${SLACKDUMP_VERSION}/slackdump_Linux_x86_64.tar.gz" \
       | tar xz -C /usr/local/bin slackdump \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY slack_log/ slack_log/
COPY scripts/ scripts/

# Shared PVC mount point — search.db (and raw/) live here.
ENV SLACK_LOG_ROOT=/data
# The container runs the team profile: the server reads search.db only and the
# refresh ETLs slackdump.sqlite straight into it. The configmap can override.
ENV SLACK_LOG_PROFILE=team
# refresh.sh cd's into /data, so `python -m slack_log.*` needs the package
# on the path regardless of cwd.
ENV PYTHONPATH=/app
EXPOSE 8770

# Default role: the web server. The CronJob overrides command to refresh.sh.
CMD ["uvicorn", "slack_log.web.app:create_app_from_env", "--factory", \
     "--host", "0.0.0.0", "--port", "8770", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
