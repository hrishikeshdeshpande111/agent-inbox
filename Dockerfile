FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY static/ ./static/

# Writable volume for the SQLite database.
VOLUME ["/app/data"]
ENV AGENT_INBOX_DB_PATH=/app/data/agent-inbox.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["python", "-m", "uvicorn", "agent_inbox.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--app-dir", "src"]
