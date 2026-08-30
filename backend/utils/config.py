"""
Configuration — loads all settings from .env

Supports two chat providers, selected by LLM_PROVIDER:
  groq   OpenAI-compatible endpoint, several API keys rotated per request
  azure  Azure OpenAI deployment

Groq has no embeddings endpoint. The vector store falls back to exact club-name
lookup, which is what the agents actually need (they search by club name, not by
semantic similarity). Generic tactical-knowledge retrieval stays disabled unless
an embedding provider is configured.
"""
import itertools
import os
import threading
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (two levels up from this file)
_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_root / ".env")


class Settings:
    # ── Provider selection ──────────────────────────────────────────────────────
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "groq").strip().lower()

    # ── Groq (OpenAI-compatible) ────────────────────────────────────────────────
    GROQ_BASE_URL: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    # Groq removed llama-3.3-70b-versatile from its catalog entirely — a
    # 404 model_not_found, not a slow deprecation window — which broke every
    # agent call with no warning. gpt-oss-120b is Groq's current largest
    # general-purpose chat model with response_format=json_object support
    # (verified directly against /v1/models and a live JSON-mode call on
    # 2026-08-31; qwen3.6-27b failed JSON validation on the same test, so it
    # is not a safe fallback). Re-check https://console.groq.com/docs/models
    # if this 404s again — Groq's catalog has already changed once with no
    # notice in this repo's history and there is no reason to expect it won't.
    GROQ_MODEL: str    = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    @property
    def GROQ_API_KEYS(self) -> list[str]:
        """Every configured Groq key, in order. Requests rotate across them."""
        raw = os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", "")
        return [k.strip() for k in raw.split(",") if k.strip()]

    # ── Azure OpenAI ────────────────────────────────────────────────────────────
    AZURE_OPENAI_API_KEY: str     = os.getenv("AZURE_OPENAI_API_KEY", "")
    AZURE_OPENAI_DEPLOYMENT_NAME: str = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4.1-mini")
    AZURE_OPENAI_API_VERSION: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

    # ── LangSmith ───────────────────────────────────────────────────────────────
    LANGSMITH_API_KEY: str     = os.getenv("LANGSMITH_API_KEY", "")
    LANGSMITH_PROJECT: str     = os.getenv("LANGCHAIN_PROJECT", "scoutly-ai")
    LANGSMITH_ENABLED: bool    = bool(os.getenv("LANGSMITH_API_KEY", ""))

    # The endpoint as provided by Azure (may include /openai/v1 suffix).
    # AsyncAzureOpenAI expects just the base host — we normalise below.
    _AZURE_OPENAI_ENDPOINT_RAW: str = os.getenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://your-resource.services.ai.azure.com/openai/v1",
    )

    @property
    def GROQ_EXTRA_CHAT_KWARGS(self) -> dict:
        """
        Extra kwargs to spread into every Groq chat completion.

        Groq's current default model (openai/gpt-oss-120b, since Groq removed
        llama-3.3-70b-versatile from its catalog entirely) is a reasoning model:
        it spends a variable share of max_tokens on internal chain-of-thought
        before writing the actual answer. A financial_analyze call that used to
        comfortably fit in 800 tokens hit max_completion_tokens with no valid
        JSON produced at all. reasoning_effort="low" cuts that overhead sharply
        — 55 tokens against 161 at the default effort on an equivalent prompt,
        confirmed directly against the API — with no loss of answer quality;
        "high" measured *worse*, producing an implausible EUR 5.5M/week salary
        from the same prompt. Scoped to Groq only: Azure's chat models do not
        accept this parameter and would likely reject the request outright.
        """
        if self.LLM_PROVIDER == "groq":
            return {"reasoning_effort": "low"}
        return {}

    @property
    def AZURE_OPENAI_ENDPOINT(self) -> str:
        """
        Return the base endpoint without /openai or /openai/v1 suffixes.
        AsyncAzureOpenAI appends its own /openai/deployments/... path.
        """
        url = self._AZURE_OPENAI_ENDPOINT_RAW.rstrip("/")
        for suffix in ("/openai/v1", "/openai"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        return url

    # The model identifier to pass on API calls, per provider.
    @property
    def OPENAI_MODEL(self) -> str:
        if self.LLM_PROVIDER == "groq":
            return self.GROQ_MODEL
        return self.AZURE_OPENAI_DEPLOYMENT_NAME

    # ── SQLite ───────────────────────────────────────────────────
    SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "data/football.db")

    @property
    def SQLITE_DB_ABSOLUTE(self) -> Path:
        """Absolute path to the SQLite database file."""
        p = Path(self.SQLITE_DB_PATH)
        return p if p.is_absolute() else _root / p

    # ── MCP Server ─────────────────────────────────────────────────────────────
    MCP_SERVER_HOST: str = os.getenv("MCP_SERVER_HOST", "localhost")
    MCP_SERVER_PORT: int = int(os.getenv("MCP_SERVER_PORT", "8005"))

    @property
    def MCP_SERVER_URL(self) -> str:
        return f"http://{self.MCP_SERVER_HOST}:{self.MCP_SERVER_PORT}/sse"

    # ── FastAPI ────────────────────────────────────────────────────────────────
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))

    # ── Paths ──────────────────────────────────────────────────────────────────
    ROOT_DIR: Path = _root
    CHARTS_DIR: Path         = _root / os.getenv("CHARTS_DIR", "charts")
    REPORTS_DIR: Path        = _root / os.getenv("REPORTS_DIR", "reports")
    FINANCIAL_PLAN_PATH: Path = _root / os.getenv("FINANCIAL_PLAN_PATH", "configs/financial_plan.yaml")
    TACTICS_PDF_PATH: Path   = _root / os.getenv("TACTICS_PDF_PATH", "data/tactics.pdf")
    PROMPTS_DIR: Path        = _root / os.getenv("PROMPTS_DIR", "prompts")
    TEMPLATES_DIR: Path      = _root / "backend" / "pdf" / "templates"

    # ── RAG knowledge base (used only by the offline loader) ───────────────────
    # The club profiles and tactical skill docs live outside the repo in the
    # original layout (a sibling of Scoutly/). Override either with an env var,
    # or drop the files under data/kb/ and they are picked up automatically.
    @property
    def KB_PROFILES_PATH(self) -> Path:
        override = os.getenv("KB_PROFILES_PATH")
        if override:
            return Path(override)
        local = _root / "data" / "kb" / "profiles_enriched.json"
        return local if local.exists() else _root.parent / "KB" / "profiles_enriched.json"

    @property
    def KB_SKILLS_DIR(self) -> Path:
        override = os.getenv("KB_SKILLS_DIR")
        if override:
            return Path(override)
        local = _root / "data" / "kb" / "skills"
        return local if local.exists() else _root.parent / "docs" / "skills"

    def __init__(self):
        # Ensure runtime directories exist
        self.CHARTS_DIR.mkdir(parents=True, exist_ok=True)
        self.REPORTS_DIR.mkdir(parents=True, exist_ok=True)


settings = Settings()

# ── Activate LangSmith auto-tracing for all LangChain calls ──────────────────
if settings.LANGSMITH_ENABLED:
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY",    settings.LANGSMITH_API_KEY)
    os.environ.setdefault("LANGCHAIN_PROJECT",    settings.LANGSMITH_PROJECT)
    os.environ.setdefault("LANGCHAIN_ENDPOINT",   "https://api.smith.langchain.com")


# ── Groq key rotation ─────────────────────────────────────────────────────────
# Groq enforces per-key rate limits, so requests are spread across every
# configured key. The cycle is process-wide and guarded because agents run
# concurrently on the SSE event loop.
_key_lock = threading.Lock()
_key_cycle: "itertools.cycle | None" = None


def next_groq_key() -> str:
    """Return the next Groq API key in round-robin order."""
    global _key_cycle
    keys = settings.GROQ_API_KEYS
    if not keys:
        raise RuntimeError(
            "No Groq API key configured. Set GROQ_API_KEYS in .env "
            "(comma-separated for rotation), or set LLM_PROVIDER=azure."
        )
    with _key_lock:
        if _key_cycle is None:
            _key_cycle = itertools.cycle(range(len(keys)))
        idx = next(_key_cycle)
    return keys[idx % len(keys)]


def _maybe_trace(client):
    if os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true" or \
       os.getenv("LANGSMITH_TRACING", "").lower() == "true":
        try:
            from langsmith import wrappers
            return wrappers.wrap_openai(client)
        except ImportError:
            pass
    return client


class RotatingGroqClient:
    """
    Async chat client that picks a different Groq key for each request.

    Exposes just the `.chat.completions.create(...)` surface the agents use. On a
    rate-limit or auth failure it retries on the remaining keys before giving up,
    so one exhausted key does not fail the run.
    """

    def __init__(self) -> None:
        from openai import AsyncOpenAI
        self._AsyncOpenAI = AsyncOpenAI
        self.chat = self._Chat(self)

    def _client(self, key: str):
        return _maybe_trace(self._AsyncOpenAI(
            api_key=key,
            base_url=settings.GROQ_BASE_URL,
            max_retries=2,
        ))

    class _Chat:
        def __init__(self, outer: "RotatingGroqClient") -> None:
            self.completions = RotatingGroqClient._Completions(outer)

    class _Completions:
        def __init__(self, outer: "RotatingGroqClient") -> None:
            self._outer = outer

        async def create(self, **kwargs):
            import asyncio
            from openai import APIStatusError, RateLimitError

            # One pass per key, then a couple more with backoff: the token budget
            # is per minute, so a short wait frees capacity that no amount of
            # key-switching will.
            attempts = max(len(settings.GROQ_API_KEYS), 1) + 2
            last: Exception | None = None
            for attempt in range(attempts):
                key = next_groq_key()
                try:
                    return await self._outer._client(key).chat.completions.create(**kwargs)
                except (RateLimitError, APIStatusError) as exc:
                    status = getattr(exc, "status_code", None)
                    # 413 here is Groq's per-minute token budget, not a payload
                    # size limit, so it is worth retrying.
                    if status not in (401, 403, 413, 429):
                        raise
                    last = exc
                    if attempt >= len(settings.GROQ_API_KEYS) - 1:
                        await asyncio.sleep(min(2 ** (attempt - len(settings.GROQ_API_KEYS) + 2), 20))
            raise last  # every key exhausted


def make_llm_client():
    """Return an async chat client for the configured provider."""
    if settings.LLM_PROVIDER == "groq":
        return RotatingGroqClient()

    from openai import AsyncAzureOpenAI
    return _maybe_trace(AsyncAzureOpenAI(
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
    ))


#: Kept for call sites that predate multi-provider support.
make_azure_client = make_llm_client


def get_langchain_llm():
    """LangChain chat model for the configured provider (used by deepagents)."""
    if settings.LLM_PROVIDER == "groq":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=settings.GROQ_MODEL,
            base_url=settings.GROQ_BASE_URL,
            api_key=next_groq_key(),
            temperature=0,
        )

    from langchain_openai import AzureChatOpenAI
    return AzureChatOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        azure_deployment=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        temperature=0,
    )


get_langchain_azure_llm = get_langchain_llm
