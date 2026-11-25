FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    DAILY_MEMORY_HOST=0.0.0.0 \
    DAILY_MEMORY_PORT=8765 \
    DAILY_MEMORY_STORAGE_ROOT=/srv/daily-memory

RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --extra-index-url https://pypi.org/simple \
    --default-timeout=60 \
    openpyxl==3.1.5 zstandard

WORKDIR /app

COPY multi_machine.py remote_client.py project_identity.py provenance.py query_memory.py write_excel.py summarizer.py export_report.py /app/
COPY server/app.py /app/server/app.py

RUN mkdir -p /srv/daily-memory/data /srv/daily-memory/DailyReport

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)" || exit 1

CMD ["python3", "-u", "/app/server/app.py"]
