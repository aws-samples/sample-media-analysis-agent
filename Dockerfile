# Amazon Linux 2023 from ECR Public, an approved internal source for base
# images. Not Docker Hub — see the container repository usage guidelines.
#
# The AL2023 default interpreter is Python 3.9, so 3.11 is installed
# explicitly and invoked as python3.11 throughout this file.
FROM --platform=linux/amd64 public.ecr.aws/amazonlinux/amazonlinux:2023

RUN dnf install -y python3.11 python3.11-pip \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# Run as an unprivileged numeric identity without requiring shadow-utils.
ARG APP_UID=1000
ARG APP_GID=1000
ENV HOME=/home/appuser
RUN mkdir -p "$HOME" \
    && chown -R ${APP_UID}:${APP_GID} "$HOME"

WORKDIR /app

COPY requirements.txt .
RUN python3.11 -m pip install --no-cache-dir -r requirements.txt

COPY . .

# Streamlit config for running behind the ALB.
#
# CAUTION: this OVERWRITES the .streamlit/config.toml copied in above, so this
# line — not the repo file — is what the container actually runs on. Editing
# only the repo file changes local development and nothing else. Keep the two
# in sync until they are consolidated (tracked in docs/threat-model.md T-07).
#
# maxUploadSize is 2000 MB, not 5120. Streamlit buffers an upload entirely in
# memory (chat_app.py reads it via uf.getbuffer(), an io.BytesIO method), so the
# binding constraint is the task's 4096 MB Memory allocation, not disk. A 5 GB
# upload could never succeed — it OOM-kills the task mid-buffer and takes every
# concurrent session on that task with it. To raise this, raise the task's
# Memory in deploy/ecs-fargate-stack.yaml first, then change both files
# together. See docs/threat-model.md T-07.
RUN mkdir -p /app/.streamlit && \
    printf '[server]\nheadless = true\nport = 8501\naddress = "0.0.0.0"\nenableCORS = false\nenableXsrfProtection = false\nmaxUploadSize = 2000\n\n[browser]\ngatherUsageStats = false\n' > /app/.streamlit/config.toml

# Hand ownership to the non-root identity for /app and its home.
RUN chown -R ${APP_UID}:${APP_GID} /app "$HOME"

USER ${APP_UID}:${APP_GID}

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python3.11", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=5)"]

CMD ["python3.11", "-m", "streamlit", "run", "chat_app.py"]
