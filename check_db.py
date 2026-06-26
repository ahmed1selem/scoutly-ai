import sqlite3
import os

db_path = "data/football.db"
print(f"File exists: {os.path.exists(db_path)}")
if os.path.exists(db_path):
    print(f"File size: {os.path.getsize(db_path)} bytes")
    conn = sqlite3.connect(db_path)
    print(conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall())
