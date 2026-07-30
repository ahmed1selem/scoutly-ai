"""Integration tests for the M1 FAISS vector store.

Requires the store to be populated (run backend.rag.loader first).
Skips gracefully if the store has not been built.
"""
import pytest
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))

import os
import socket
from pathlib import Path
from urllib.parse import urlparse

STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "vectorstore"
STORE_BUILT = (STORE_PATH / "clubs" / "index.faiss").exists()


def _embeddings_reachable() -> bool:
    """
    Similarity search embeds the query, so it needs a live Azure endpoint.
    Distinguish "the deployment is unreachable" from "the code is broken" —
    otherwise an expired Azure resource shows up as five red vector-store tests.
    """
    from backend.utils.config import settings  # loads .env

    endpoint = os.getenv("AZURE_OPENAI_EMBEDDING_ENDPOINT") or settings.AZURE_OPENAI_ENDPOINT
    host = urlparse(endpoint).hostname
    if not host:
        return False
    try:
        socket.getaddrinfo(host, 443)
        return True
    except (socket.gaierror, OSError):
        return False


EMBEDDINGS_UP = _embeddings_reachable()
needs_embeddings = pytest.mark.skipif(
    not EMBEDDINGS_UP,
    reason="Azure embedding endpoint unreachable — check AZURE_OPENAI_EMBEDDING_ENDPOINT in .env",
)


@pytest.fixture(scope="module")
def vs():
    from backend.rag.vectorstore import ClubVectorStore
    return ClubVectorStore()

@pytest.mark.skipif(not STORE_BUILT, reason="vectorstore not built — run python -m backend.rag.loader")
def test_clubs_count(vs):
    assert vs.clubs_count() == 96

@pytest.mark.skipif(not STORE_BUILT, reason="vectorstore not built")
def test_knowledge_count_nonzero(vs):
    assert vs.knowledge_count() > 0

@pytest.mark.skipif(not STORE_BUILT, reason="vectorstore not built")
@needs_embeddings
def test_search_clubs_returns_results(vs):
    results = vs.search_clubs("high press possession attacking", n_results=3)
    assert len(results) > 0

@pytest.mark.skipif(not STORE_BUILT, reason="vectorstore not built")
@needs_embeddings
def test_search_clubs_have_required_fields(vs):
    results = vs.search_clubs("counter attack", n_results=1)
    club = results[0]
    assert "club" in club
    assert "recruitment_profile" in club
    assert "team_identity" in club

@pytest.mark.skipif(not STORE_BUILT, reason="vectorstore not built")
@needs_embeddings
def test_get_club_by_id(vs):
    # Find a real club_id first via search
    results = vs.search_clubs("Manchester City possession", n_results=1)
    assert results, "No clubs found"
    club_id = results[0]["club"]["club_id"]
    fetched = vs.get_club_by_id(club_id)
    assert fetched is not None
    assert fetched["club"]["club_id"] == club_id

@pytest.mark.skipif(not STORE_BUILT, reason="vectorstore not built")
def test_get_club_by_nonexistent_id_returns_none(vs):
    result = vs.get_club_by_id("definitely_not_a_real_club_id_xyz")
    assert result is None

@pytest.mark.skipif(not STORE_BUILT, reason="vectorstore not built")
@needs_embeddings
def test_knowledge_retrieval(vs):
    docs = vs.get_tactical_knowledge("high press striker", n_results=2)
    assert len(docs) > 0
    assert all(isinstance(d, str) for d in docs)
    assert any("press" in d.lower() for d in docs)

@pytest.mark.skipif(not STORE_BUILT, reason="vectorstore not built")
@needs_embeddings
def test_club_has_ideal_player_traits(vs):
    results = vs.search_clubs("striker box movement", n_results=3)
    found_traits = False
    for club in results:
        traits = club.get("recruitment_profile", {}).get("ideal_player_traits", {})
        if any(len(v) > 0 for v in traits.values()):
            found_traits = True
            break
    assert found_traits, "No clubs found with non-empty ideal_player_traits"
