# ── Scoutly AI — Single-Container Build (Fly.io / demo deployment) ─────────────
# Runs BOTH the FastAPI backend (port 8000) and the FastMCP server (port 8001)
# in one container using a shell startup script.
# FBref data comes from the pre-built SQLite cache (data/fbref_cache.db),
# so no Chrome / botasaurus is needed at runtime.
# ─────────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # WeasyPrint (PDF rendering)
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    # SQLite
    sqlite3 \
    libsqlite3-dev \
    # Misc build
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# ── vendored deepagents ───────────────────────────────────────────────────────
COPY vendor/deepagents /usr/local/lib/python3.11/site-packages/deepagents

# ── Python deps ───────────────────────────────────────────────────────────────
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    aiofiles \
    openai \
    langchain \
    "langchain-openai" \
    "langchain-community" \
    faiss-cpu \
    "tavily-python" \
    pandas \
    numpy \
    pydantic \
    python-dotenv \
    pyyaml \
    weasyprint \
    jinja2 \
    matplotlib \
    seaborn \
    plotly \
    httpx \
    "mcp>=1.0.0" \
    langsmith \
    langchain-core \
    langchain-text-splitters \
    aiosqlite \
    requests \
    lxml \
    beautifulsoup4

# ── App source ────────────────────────────────────────────────────────────────
COPY backend/    ./backend/
COPY prompts/    ./prompts/
COPY frontend/   ./frontend/
COPY configs/    ./configs/
COPY data/       ./data/

# ── Runtime dirs ──────────────────────────────────────────────────────────────
RUN mkdir -p charts reports

# ── Startup script ────────────────────────────────────────────────────────────
COPY start.sh ./start.sh
RUN chmod +x start.sh

EXPOSE 8000

# MCP server on :8001 is internal — not exposed externally
CMD ["./start.sh"]
