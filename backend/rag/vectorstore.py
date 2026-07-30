"""
Vector store for Scoutly-AI — FAISS-backed, Azure OpenAI embeddings.

Two logical collections:
  clubs            — one doc per club: embedded prose (narrative + evidence),
                     structured payload stored in a companion JSON file.
  tactical_knowledge — chunked skill docs for generic tactical RAG.

The interface (get_club_by_id / search_clubs / get_tactical_knowledge) is
identical to the original ChromaDB version so no agent code changes.
"""
import json
import os
from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_openai import AzureOpenAIEmbeddings

from ..utils.config import settings


_instance: "ClubVectorStore | None" = None


def get_vectorstore() -> "ClubVectorStore":
    """Return the process-level singleton — loads from disk only once."""
    global _instance
    if _instance is None:
        _instance = ClubVectorStore()
    return _instance


class ClubVectorStore:
    def __init__(self, db_path: str = None):
        base = Path(db_path) if db_path else settings.ROOT_DIR / "data" / "vectorstore"
        base.mkdir(parents=True, exist_ok=True)

        self._clubs_path     = base / "clubs"
        self._knowledge_path = base / "knowledge"
        self._payloads_file  = base / "club_payloads.json"

        emb_endpoint   = os.getenv("AZURE_OPENAI_EMBEDDING_ENDPOINT",    settings.AZURE_OPENAI_ENDPOINT)
        emb_key        = os.getenv("AZURE_OPENAI_EMBEDDING_API_KEY",      settings.AZURE_OPENAI_API_KEY)
        emb_deployment = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME", "text-embedding-3-small")

        self.embeddings = AzureOpenAIEmbeddings(
            azure_endpoint=emb_endpoint,
            azure_deployment=emb_deployment,
            api_key=emb_key,
            openai_api_version=settings.AZURE_OPENAI_API_VERSION,
        )

        # Payload dict: club_id → full structured profile (not embedded)
        self._payloads: dict[str, dict] = {}
        if self._payloads_file.exists():
            with open(self._payloads_file, encoding="utf-8") as f:
                self._payloads = json.load(f)

        # Lazily loaded FAISS indexes (None until first access or load)
        self._clubs_vs:     FAISS | None = None
        self._knowledge_vs: FAISS | None = None

    # ── Internal helpers ────────────────────────────────────────────────────────

    def _load_clubs_vs(self) -> FAISS | None:
        if self._clubs_vs is None and (self._clubs_path / "index.faiss").exists():
            self._clubs_vs = FAISS.load_local(
                str(self._clubs_path), self.embeddings,
                allow_dangerous_deserialization=True,
            )
        return self._clubs_vs

    def _load_knowledge_vs(self) -> FAISS | None:
        if self._knowledge_vs is None and (self._knowledge_path / "index.faiss").exists():
            self._knowledge_vs = FAISS.load_local(
                str(self._knowledge_path), self.embeddings,
                allow_dangerous_deserialization=True,
            )
        return self._knowledge_vs

    def _save_payloads(self):
        with open(self._payloads_file, "w", encoding="utf-8") as f:
            json.dump(self._payloads, f, ensure_ascii=False)

    # ── Build methods (called by loader.py) ─────────────────────────────────────

    def add_clubs(self, ids: list[str], texts: list[str], payloads: list[dict]):
        """Add (or upsert) club documents. texts = narrative+evidence; payloads = full structured records."""
        metadatas = [
            {
                "club_id":      p.get("club", {}).get("club_id", i),
                "league":       p.get("club", {}).get("league", ""),
                "primary_style": p.get("team_identity", {}).get("primary_style", {}).get("value", ""),
            }
            for i, p in zip(ids, payloads)
        ]

        if self._clubs_vs is None and not (self._clubs_path / "index.faiss").exists():
            self._clubs_vs = FAISS.from_texts(texts, self.embeddings, metadatas=metadatas)
        else:
            vs = self._load_clubs_vs()
            vs.add_texts(texts, metadatas=metadatas)

        # Persist
        self._clubs_path.mkdir(parents=True, exist_ok=True)
        self._clubs_vs.save_local(str(self._clubs_path))

        # Store structured payloads separately (not embedded)
        for cid, payload in zip(ids, payloads):
            self._payloads[cid] = payload
        self._save_payloads()

    def add_knowledge(self, ids: list[str], texts: list[str], metadatas: list[dict]):
        """Add tactical-knowledge chunks."""
        if self._knowledge_vs is None and not (self._knowledge_path / "index.faiss").exists():
            self._knowledge_vs = FAISS.from_texts(texts, self.embeddings, metadatas=metadatas)
        else:
            vs = self._load_knowledge_vs()
            vs.add_texts(texts, metadatas=metadatas)

        self._knowledge_path.mkdir(parents=True, exist_ok=True)
        self._knowledge_vs.save_local(str(self._knowledge_path))

    # ── Query methods (called by agents) ───────────────────────────────────────

    def get_club_by_id(self, club_id: str) -> dict | None:
        """Return the full structured payload for a specific club (exact match)."""
        return self._payloads.get(club_id)

    def _search_clubs_by_name(self, query: str, n_results: int) -> list[dict]:
        """
        Match the query against club names directly.

        The agents always search for a named club ("Liverpool"), so an exact or
        substring name match is both more accurate than nearest-neighbour search
        and free of any embedding call — which matters because the chat provider
        may not offer an embeddings endpoint at all.
        """
        q = query.strip().lower()
        if not q:
            return []

        exact, partial = [], []
        for payload in self._payloads.values():
            name = payload.get("club", {}).get("club_name", "")
            low = name.lower()
            if low == q:
                exact.append(payload)
            elif low in q or q in low:
                partial.append(payload)

        # Longer names first, so "Manchester United" beats "Manchester City"
        # when both are substrings of the query.
        partial.sort(key=lambda p: len(p.get("club", {}).get("club_name", "")), reverse=True)
        return (exact + partial)[:n_results]

    def search_clubs(self, query: str, n_results: int = 5, filters: dict = None) -> list[dict]:
        """Find club profiles by name first, then fall back to semantic search."""
        by_name = self._search_clubs_by_name(query, n_results)
        if by_name:
            return by_name

        try:
            vs = self._load_clubs_vs()
        except Exception:
            return []
        if vs is None:
            return []
        try:
            docs = vs.similarity_search(query, k=n_results)
        except Exception:
            # No reachable embeddings provider — name lookup above is the only path.
            return []
        results = []
        for doc in docs:
            club_id = doc.metadata.get("club_id")
            payload = self._payloads.get(club_id)
            if payload:
                results.append(payload)
        return results

    def get_tactical_knowledge(self, query: str, n_results: int = 3, filters: dict = None) -> list[str]:
        """
        Semantic search on generic tactical-knowledge docs.

        Unlike club lookup this genuinely needs embeddings; with no reachable
        provider it returns nothing rather than failing the run, and the tactical
        agent proceeds on the club profile alone.
        """
        try:
            vs = self._load_knowledge_vs()
            if vs is None:
                return []
            return [d.page_content for d in vs.similarity_search(query, k=n_results)]
        except Exception:
            return []

    # ── Compat: expose collection-like count for tests ──────────────────────────

    def clubs_count(self) -> int:
        return len(self._payloads)

    def knowledge_count(self) -> int:
        vs = self._load_knowledge_vs()
        return vs.index.ntotal if vs else 0
