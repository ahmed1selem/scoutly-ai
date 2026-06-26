"""
Configuration — loads all settings from .env

Uses Azure OpenAI (AsyncAzureOpenAI) instead of the public OpenAI API.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (two levels up from this file)
_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_root / ".env")


class Settings:
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

    # Alias — the model name for API calls is the deployment name
    @property
    def OPENAI_MODEL(self) -> str:
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


def make_azure_client():
    """
    Create and return a configured AsyncAzureOpenAI client.
    Import here to avoid circular imports.
    """
    from openai import AsyncAzureOpenAI
    client = AsyncAzureOpenAI(
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
    )

    if os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true" or os.getenv("LANGSMITH_TRACING", "").lower() == "true":
        try:
            from langsmith import wrappers
            client = wrappers.wrap_openai(client)
        except ImportError:
            pass

    return client

def get_langchain_azure_llm():
    from langchain_openai import AzureChatOpenAI
    return AzureChatOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        azure_deployment=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        temperature=0,
    )
