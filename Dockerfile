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
# Install in two layers — heavy packages first (better layer caching)
RUN pip install --no-cache-dir \
    "langchain>=0.3.0" \
    "langchain-core>=0.3.0" \
    "langchain-openai>=0.2.0" \
    "langchain-community>=0.3.0" \
    "langchain-anthropic>=0.3.0" \
    "langchain-google-genai>=2.0.0" \
    "langchain-text-splitters>=0.3.0" \
    faiss-cpu \
    pandas \
    numpy \
    matplotlib \
    seaborn \
    plotly

RUN pip install --no-cache-dir \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.30.0" \
    aiofiles \
    "openai>=1.40.0" \
    "tavily-python>=0.5.0" \
    "pydantic>=2.0" \
    python-dotenv \
    pyyaml \
    "weasyprint>=60.0" \
    "jinja2>=3.1" \
    "httpx>=0.27.0" \
    "mcp>=1.0.0" \
    "langsmith>=0.1.0" \
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
