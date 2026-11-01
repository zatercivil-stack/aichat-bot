FROM python:3.12-slim AS base
WORKDIR /app
ARG APP_UID=10001
ARG APP_GID=10001
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/* \
 && test "$APP_UID" -gt 0 && test "$APP_GID" -gt 0 \
 && groupadd --gid "$APP_GID" app \
 && useradd --uid "$APP_UID" --gid app --create-home app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
FROM base AS test
ENV PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache"
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY --chown=app:app . .
USER app
CMD ["pytest"]

FROM base AS runtime
COPY --chown=app:app . .
RUN chown -R app:app /app
USER app
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1
CMD ["python", "-m", "main"]
