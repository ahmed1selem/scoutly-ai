from backend.rag.vectorstore import ClubVectorStore
import sys

print("Initializing ClubVectorStore...")
sys.stdout.flush()
try:
    vs = ClubVectorStore()
    print("Success!")
except Exception as e:
    print("Exception:", e)
