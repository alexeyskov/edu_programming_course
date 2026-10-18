# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS sandbox-builder
RUN apt-get update && apt-get install --no-install-recommends -y gcc libc6-dev linux-libc-dev && rm -rf /var/lib/apt/lists/*
COPY sandbox.c /build/sandbox.c
RUN gcc -O2 -Wall -Wextra -Werror -o /build/edu-clangd-sandbox /build/sandbox.c

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CODE_INTELLIGENCE_WORK_ROOT=/var/lib/code-intelligence/workspaces
RUN apt-get update && apt-get install --no-install-recommends -y clangd clang gcc g++ tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 language-service \
    && useradd --uid 65532 --gid 65532 --no-create-home --home-dir /nonexistent language-service \
    && install -d -o 65532 -g 65532 -m 0700 /var/lib/code-intelligence/workspaces
COPY --from=sandbox-builder /build/edu-clangd-sandbox /usr/local/bin/edu-clangd-sandbox
WORKDIR /opt/language-service
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .
USER 65532:65532
EXPOSE 8084
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8084/health/ready', timeout=2).read()"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "edu_language.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8084", "--workers", "1", "--no-access-log", "--no-server-header"]
