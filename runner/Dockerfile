# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    RUNNER_EXECUTOR=local \
    RUNNER_WORK_ROOT=/var/lib/edu-runner/jobs

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        ca-certificates \
        clang \
        gcc \
        g++ \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 runner \
    && useradd --uid 65532 --gid 65532 --no-create-home --home-dir /nonexistent runner \
    && install -d -o runner -g runner -m 0700 /var/lib/edu-runner/jobs

WORKDIR /opt/runner
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

USER 65532:65532
EXPOSE 8081

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health/ready', timeout=2).read()"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "edu_runner.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8081", "--workers", "1", "--no-access-log", "--no-server-header"]
