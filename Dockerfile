FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/db.sqlite \
    UPLOAD_FOLDER=/data/uploads \
    MAGISK_TEMPLATE_DIR=/app/magisk-template \
    WINDOWS_TEMPLATE_DIR=/app/windows-client

WORKDIR /app
COPY server/app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY server/app/ /app/
COPY clients/magisk/ /app/magisk-template/
COPY clients/windows/ /app/windows-client/
RUN mkdir -p /data/uploads

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)" || exit 1

CMD ["gunicorn", "--worker-class", "gthread", "--workers", "1", "--threads", "100", "--bind", "0.0.0.0:5000", "--timeout", "90", "--graceful-timeout", "30", "app:app"]
