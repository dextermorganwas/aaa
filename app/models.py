from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

@dataclass(frozen=True)
class Lookup:
    media_type: str
    tmdb_id: Optional[str] = None
    imdb_id: Optional[str] = None
    tvdb_id: Optional[str] = None

@dataclass
class Candidate:
    provider: str
    url: str
    language: Optional[str] = None
    kind: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    label: Optional[str] = None
    score: Optional[float] = None
    permanent: bool = False
    metadata: dict = field(default_factory=dict)

@dataclass
class Resolution:
    media_type: str
    art_type: str
    candidate: Candidate
    item_name: Optional[str] = None
    original_language: Optional[str] = None
    fetched_url: Optional[str] = None
    content_type: str = 'image/jpeg'

@dataclass
class ProviderState:
    provider: str
    key: str
    failed_at: Optional[datetime] = None
