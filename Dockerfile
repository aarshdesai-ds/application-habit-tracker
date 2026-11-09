FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

# Run as a normal user, not root
RUN useradd -m -u 1000 user
USER user

# APPDATA: the servers keep their SQLite files here (wiped on every restart / redeploy).
# UV_FROZEN: install exactly what uv.lock says; never re-resolve in the cloud.
ENV HOME=/home/user \
    APPDATA=/home/user/data \
    MCP_SERVERS_DIR=/home/user/app/servers \
    DEMO_MODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /home/user/app
COPY --chown=user . .

# chmod: folders copied from Windows/OneDrive can arrive read-only, which stops uv creating .venv
RUN chmod -R u+w /home/user/app \
 && mkdir -p "$APPDATA" \
 && uv sync --directory servers/habit-check \
 && uv sync --directory servers/job-tracker \
 && uv sync --directory client

# Render tells the app which port to use through $PORT (default 10000); 7860 is the local fallback.
EXPOSE 7860
CMD ["sh", "-c", "exec uv run --directory client uvicorn web:app --host 0.0.0.0 --port ${PORT:-7860}"]
