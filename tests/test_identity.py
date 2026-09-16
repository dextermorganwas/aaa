import asyncio
import sqlite3
from pathlib import Path

def test_schema_has_canonical_key():
    text=Path('app/db.py').read_text()
    assert 'canonical_key TEXT NOT NULL UNIQUE' in text
    assert 'identity = lookup.tmdb_id or lookup.imdb_id or lookup.tvdb_id or' in text
