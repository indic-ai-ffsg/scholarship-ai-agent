FROM python:3.12-slim
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY main.py server.py ./
COPY src/ ./src/

RUN addgroup --system --gid 10001 discovery \
 && adduser  --system --uid 10001 --gid 10001 --no-create-home discovery \
 && chmod -R a+rX /ms-playwright

USER discovery:discovery

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8765"]
