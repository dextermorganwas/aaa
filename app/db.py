import json
from datetime import datetime, timezone
from .util import safe_key
from pathlib import Path
import aiosqlite

SCHEMA = '''
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_key TEXT NOT NULL UNIQUE,
  media_type TEXT NOT NULL,
  tmdb_id TEXT NOT NULL DEFAULT '',
  imdb_id TEXT NOT NULL DEFAULT '',
  tvdb_id TEXT NOT NULL DEFAULT '',
  title TEXT,
  original_language TEXT,
  release_year INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(media_type, tmdb_id, imdb_id, tvdb_id)
);
CREATE TABLE IF NOT EXISTS selections (
  item_id INTEGER NOT NULL,
  art_type TEXT NOT NULL,
  provider TEXT,
  source_url TEXT,
  local_path TEXT,
  content_type TEXT,
  selected_at TEXT NOT NULL,
  expires_at TEXT,
  is_override INTEGER NOT NULL DEFAULT 0,
  resolver_version INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(item_id, art_type),
  FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS candidates (
  item_id INTEGER NOT NULL,
  art_type TEXT NOT NULL,
  provider TEXT NOT NULL,
  url TEXT NOT NULL,
  language TEXT,
  kind TEXT,
  width INTEGER,
  height INTEGER,
  label TEXT,
  score REAL,
  permanent INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT,
  discovered_at TEXT NOT NULL,
  PRIMARY KEY(item_id, art_type, provider, url),
  FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS failures (
  provider TEXT NOT NULL,
  key TEXT NOT NULL,
  failed_at TEXT NOT NULL,
  message TEXT,
  PRIMARY KEY(provider, key)
);
CREATE INDEX IF NOT EXISTS idx_items_title ON items(title);
'''

class Database:
    def __init__(self, path: Path):
        self.path = path
        self.db: aiosqlite.Connection | None = None

    async def connect(self):
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self._migrate()
        await self.db.commit()

    async def _migrate(self):
        # SQLite schemas from earlier starter versions do not have these columns.
        cur = await self.db.execute("PRAGMA table_info(items)")
        item_cols = {row[1] for row in await cur.fetchall()}
        if 'release_year' not in item_cols:
            await self.db.execute('ALTER TABLE items ADD COLUMN release_year INTEGER')
        cur = await self.db.execute("PRAGMA table_info(selections)")
        selection_cols = {row[1] for row in await cur.fetchall()}
        if 'resolver_version' not in selection_cols:
            await self.db.execute('ALTER TABLE selections ADD COLUMN resolver_version INTEGER NOT NULL DEFAULT 0')

    async def close(self):
        if self.db:
            await self.db.close()

    async def upsert_item(self, lookup, title=None, original_language=None, release_year=None):
        now = datetime.now(timezone.utc).isoformat()
        identity = lookup.tmdb_id or lookup.imdb_id or lookup.tvdb_id or ''
        canonical_key = safe_key(lookup.media_type, identity)
        # Prefer the strongest available identity when possible so a request that starts with only TMDB
        # can later be enriched with IMDb/TVDB IDs without creating a second item.
        vals=(canonical_key, lookup.media_type, lookup.tmdb_id or '', lookup.imdb_id or '', lookup.tvdb_id or '', title, original_language, release_year, now, now)
        q = '''INSERT INTO items(canonical_key,media_type,tmdb_id,imdb_id,tvdb_id,title,original_language,release_year,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(canonical_key) DO UPDATE SET
               tmdb_id=CASE WHEN excluded.tmdb_id<>'' THEN excluded.tmdb_id ELSE items.tmdb_id END,
               imdb_id=CASE WHEN excluded.imdb_id<>'' THEN excluded.imdb_id ELSE items.imdb_id END,
               tvdb_id=CASE WHEN excluded.tvdb_id<>'' THEN excluded.tvdb_id ELSE items.tvdb_id END,
               title=COALESCE(excluded.title, items.title),
               original_language=COALESCE(excluded.original_language, items.original_language),
               release_year=COALESCE(excluded.release_year, items.release_year),
               updated_at=excluded.updated_at'''
        await self.db.execute(q, vals)
        await self.db.commit()
        cur = await self.db.execute('SELECT * FROM items WHERE canonical_key=?', (canonical_key,))
        return await cur.fetchone()

    async def get_selection(self, item_id, art_type):
        cur = await self.db.execute('SELECT * FROM selections WHERE item_id=? AND art_type=?', (item_id, art_type))
        return await cur.fetchone()

    async def set_selection(self, item_id, art_type, provider, source_url, local_path, content_type, expires_at=None, override=False, resolver_version=0):
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute('''INSERT INTO selections(item_id,art_type,provider,source_url,local_path,content_type,selected_at,expires_at,is_override,resolver_version)
          VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(item_id,art_type) DO UPDATE SET provider=excluded.provider,source_url=excluded.source_url,
          local_path=excluded.local_path,content_type=excluded.content_type,selected_at=excluded.selected_at,
          expires_at=excluded.expires_at,is_override=excluded.is_override,resolver_version=excluded.resolver_version''',
          (item_id, art_type, provider, source_url, local_path, content_type, now, expires_at, int(override), int(resolver_version)))
        await self.db.commit()

    async def clear_selection(self, item_id, art_type):
        await self.db.execute('DELETE FROM selections WHERE item_id=? AND art_type=?', (item_id, art_type))
        await self.db.commit()

    async def save_candidates(self, item_id, art_type, candidates):
        now = datetime.now(timezone.utc).isoformat()
        for c in candidates:
            await self.db.execute('''INSERT OR REPLACE INTO candidates(item_id,art_type,provider,url,language,kind,width,height,label,score,permanent,metadata_json,discovered_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (item_id, art_type, c.provider, c.url, c.language, c.kind, c.width, c.height, c.label, c.score, int(c.permanent), json.dumps(c.metadata), now))
        await self.db.commit()

    async def list_candidates(self, item_id, art_type):
        cur = await self.db.execute('SELECT * FROM candidates WHERE item_id=? AND art_type=? ORDER BY provider, discovered_at', (item_id, art_type))
        return await cur.fetchall()

    async def list_items(self, q='', limit=100):
        cur = await self.db.execute('''SELECT i.*, GROUP_CONCAT(s.art_type || ':' || COALESCE(s.provider,'')) AS selections
          FROM items i LEFT JOIN selections s ON s.item_id=i.id
          WHERE (?='' OR lower(i.title) LIKE lower(?))
          GROUP BY i.id ORDER BY i.updated_at DESC LIMIT ?''', (q, f'%{q}%', limit))
        return await cur.fetchall()

    async def get_item(self, item_id):
        cur = await self.db.execute('SELECT * FROM items WHERE id=?', (item_id,))
        return await cur.fetchone()

    async def is_failure_cooldown(self, provider, key, cooldown_seconds):
        cur = await self.db.execute('SELECT failed_at FROM failures WHERE provider=? AND key=?', (provider, key))
        row = await cur.fetchone()
        if not row:
            return False
        failed = datetime.fromisoformat(row['failed_at'])
        return (datetime.now(timezone.utc) - failed).total_seconds() < cooldown_seconds

    async def record_failure(self, provider, key, message):
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute('INSERT OR REPLACE INTO failures(provider,key,failed_at,message) VALUES(?,?,?,?)', (provider,key,now,message[:500]))
        await self.db.commit()

    async def clear_failure(self, provider, key):
        await self.db.execute('DELETE FROM failures WHERE provider=? AND key=?', (provider,key))
        await self.db.commit()
