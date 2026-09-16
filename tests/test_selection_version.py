import asyncio
from pathlib import Path

import aiosqlite
import pytest

from app.db import Database


@pytest.mark.asyncio
async def test_selection_schema_migrates_resolver_version(tmp_path: Path):
    path = tmp_path / 'legacy.sqlite3'
    async with aiosqlite.connect(path) as conn:
        await conn.executescript('''
        CREATE TABLE items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          canonical_key TEXT NOT NULL UNIQUE,
          media_type TEXT NOT NULL,
          tmdb_id TEXT NOT NULL DEFAULT '',
          imdb_id TEXT NOT NULL DEFAULT '',
          tvdb_id TEXT NOT NULL DEFAULT '',
          title TEXT,
          original_language TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE selections (
          item_id INTEGER NOT NULL,
          art_type TEXT NOT NULL,
          provider TEXT,
          source_url TEXT,
          local_path TEXT,
          content_type TEXT,
          selected_at TEXT NOT NULL,
          expires_at TEXT,
          is_override INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(item_id, art_type)
        );
        ''')
        await conn.commit()
    db = Database(path)
    await db.connect()
    cur = await db.db.execute('PRAGMA table_info(selections)')
    cols = {row[1] for row in await cur.fetchall()}
    cur = await db.db.execute('PRAGMA table_info(items)')
    item_cols = {row[1] for row in await cur.fetchall()}
    assert 'resolver_version' in cols
    assert 'release_year' in item_cols
    await db.close()
