# syntax=docker/dockerfile:1.7
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/moodle-browser
COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
    && chown -R pwuser:pwuser /opt/moodle-browser \
    && test "$(id -u pwuser):$(id -g pwuser)" = "1001:1001"

USER pwuser
EXPOSE 8083

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8083/health/ready', timeout=2).read()"]

CMD ["uvicorn", "moodle_browser.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8083", "--workers", "1", "--no-access-log", "--no-server-header"]
