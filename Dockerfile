FROM python:3.12-slim

# Stdlib only — no pip install step, no requirements.txt.
WORKDIR /app

COPY claude_cache_dashboard.py /app/

# Claude Code logs get mounted read-only here; history is persisted here.
ENV CLAUDE_PROJECTS_DIR=/logs \
    HISTORY_FILE=/data/history.json \
    PORT=8080 \
    HOST=0.0.0.0 \
    DAYS=30 \
    REFRESH=60 \
    TZ=Asia/Jakarta \
    PYTHONUNBUFFERED=1

# Non-root. UID 1000 so the mounted /data volume stays writable.
RUN useradd -u 1000 -m tracker && mkdir -p /data /logs && chown -R tracker /data /app
USER tracker

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=2).status==200 else 1)"

CMD ["python", "claude_cache_dashboard.py", "--serve"]
