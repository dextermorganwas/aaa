import asyncio
from types import SimpleNamespace
from pathlib import Path

from app.models import Candidate
from app.resolver import Resolver


def settings(tmp_path):
    return SimpleNamespace(
        http_timeout_seconds=2,
        max_concurrent_provider_requests=4,
        tpdb_timeout_seconds=1,
        tpdb_background_refresh=True,
        tpdb_cache_ttl_seconds=0,
        cache_ttl_seconds=100,
        failure_cooldown_seconds=10,
        max_retries=0,
        retry_backoff_seconds=0,
        max_image_bytes=1000000,
        image_dir=tmp_path,
        tpdb_matcher_version=3,
    )


class FakeDB:
    def __init__(self):
        self.selection = None
        self.calls = []
    async def get_selection(self, item_id, art_type):
        return self.selection
    async def set_selection(self, item_id, art_type, provider, source_url, local_path, content_type, expires_at=None, override=False, resolver_version=0):
        self.selection = {
            'provider': provider, 'source_url': source_url, 'local_path': local_path,
            'content_type': content_type, 'expires_at': expires_at, 'is_override': int(override),
            'resolver_version': resolver_version,
        }
    async def record_failure(self,*a): pass
    async def clear_failure(self,*a): pass


def test_cache_path_changes_when_source_url_changes(tmp_path):
    db = FakeDB()
    r = Resolver.__new__(Resolver)
    r.settings = settings(tmp_path)
    db = db
    r.db = db
    a = Candidate('theposterdb','https://theposterdb.com/api/assets/1',kind='poster')
    b = Candidate('theposterdb','https://theposterdb.com/api/assets/2',kind='poster')
    assert r._path(1,'poster',a) != r._path(1,'poster',b)


class R(Resolver):
    pass
