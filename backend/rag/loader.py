"""
M1 offline build — loads club profiles and tactical-knowledge docs into the vector store.

Run once (or on a schedule) before serving requests:
    python -m backend.rag.loader
"""
import json
import sys
from pathlib import Path

from langchain_text_splitters import MarkdownTextSplitter

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from backend.rag.vectorstore import ClubVectorStore
from backend.utils.config import settings


def load_clubs(vectorstore: ClubVectorStore, kb_path: Path):
    print(f"Loading clubs from {kb_path}...")
    with open(kb_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    profiles = data.get("profiles", [])
    print(f"Found {len(profiles)} profiles.")

    batch_size = 10
    for i in range(0, len(profiles), batch_size):
        batch = profiles[i : i + batch_size]
        ids, texts, payloads = [], [], []

        for p in batch:
            club_id = p.get("club", {}).get("club_id")
            if not club_id:
                continue
            narrative  = p.get("profile_narrative", "")
            ev_text    = "\n".join(e.get("text_summary", "") for e in p.get("evidence", []))
            full_text  = f"{narrative}\n\nEvidence:\n{ev_text}".strip()

            ids.append(club_id)
            texts.append(full_text)
            payloads.append(p)

        if ids:
            print(f"  Embedding + storing batch {i // batch_size + 1} ({len(ids)} clubs)...")
            try:
                vectorstore.add_clubs(ids, texts, payloads)
                print(f"  OK — {[p.get('club', {}).get('club_name') for p in payloads[:3]]}")
            except Exception as e:
                print(f"  ERROR in batch: {e}")

    print(f"Clubs loaded. Total in store: {vectorstore.clubs_count()}")


def load_skills(vectorstore: ClubVectorStore, skills_dir: Path):
    print(f"Loading tactical knowledge from {skills_dir}...")
    if not skills_dir.exists():
        print(f"Skills directory not found: {skills_dir}")
        return

    splitter = MarkdownTextSplitter(chunk_size=400, chunk_overlap=50)
    ids, texts, metadatas = [], [], []

    for md_file in sorted(skills_dir.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        chunks  = splitter.split_text(content)
        for j, chunk in enumerate(chunks):
            ids.append(f"{md_file.stem}_{j}")
            texts.append(chunk)
            metadatas.append({"source": md_file.name, "type": "skill"})

    if not ids:
        print("No skill docs found.")
        return

    batch_size = 10
    for i in range(0, len(ids), batch_size):
        b_ids  = ids[i : i + batch_size]
        b_txt  = texts[i : i + batch_size]
        b_meta = metadatas[i : i + batch_size]
        print(f"  Embedding knowledge batch {i // batch_size + 1} ({len(b_ids)} chunks)...")
        try:
            vectorstore.add_knowledge(b_ids, b_txt, b_meta)
        except Exception as e:
            print(f"  ERROR: {e}")

    print(f"Knowledge loaded. Total chunks: {vectorstore.knowledge_count()}")


if __name__ == "__main__":
    kb_path    = settings.KB_PROFILES_PATH
    skills_dir = settings.KB_SKILLS_DIR

    if not kb_path.exists():
        print(
            f"Club profiles not found at {kb_path}.\n"
            "Set KB_PROFILES_PATH in .env, or place the file at data/kb/profiles_enriched.json."
        )
        sys.exit(1)

    vs = ClubVectorStore()
    load_clubs(vs, kb_path)
    load_skills(vs, skills_dir)
