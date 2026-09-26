FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libarchive-tools \
    && apt-get clean \
    && addgroup --system app && adduser --system --ingroup app app
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

COPY --chown=app:app . .
RUN chmod +x /app/entrypoint.sh
USER app

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
