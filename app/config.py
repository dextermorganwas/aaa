from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    public_base_url: str = 'http://localhost:8099'
    tmdb_bearer_token: str = ''
    tvdb_api_key: str = ''
    tvdb_user_pin: str = ''

    host: str = '0.0.0.0'
    port: int = 8099
    log_level: str = 'INFO'

    tmdb_poster_size: str = 'w500'
    tmdb_backdrop_size: str = 'original'
    tmdb_logo_size: str = 'original'

    http_timeout_seconds: float = 6.0
    tpdb_timeout_seconds: float = 4.0
    tpdb_background_refresh: bool = True
    tpdb_search_pages: int = 2
    tpdb_max_targets: int = 8
    tpdb_matcher_version: int = 2
    max_retries: int = 2
    retry_backoff_seconds: float = 0.35
    max_concurrent_provider_requests: int = 8
    max_image_bytes: int = 15_000_000

    data_dir: Path = Path('/data')
    cache_ttl_seconds: int = 2_592_000
    tpdb_cache_ttl_seconds: int = 0
    failure_cooldown_seconds: int = 300
    negative_cache_ttl_seconds: int = 900

    admin_enabled: bool = True
    admin_token: str = ''
    admin_session_hours: int = 12
    admin_cookie_secure: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / 'art_proxy.sqlite3'

    @property
    def image_dir(self) -> Path:
        return self.data_dir / 'images'

@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.image_dir.mkdir(parents=True, exist_ok=True)
    return s
