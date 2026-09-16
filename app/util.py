import hashlib
import re
from urllib.parse import urlparse

ALLOWED_TYPES = {'movie', 'series'}
ALLOWED_ART = {'poster', 'backdrop', 'logo'}


def parse_art_request(art_type: str, spec: str):
    if art_type not in ALLOWED_ART:
        raise ValueError('unsupported artwork type')
    if spec.lower().endswith('.jpg'):
        spec = spec[:-4]
    elif spec.lower().endswith('.jpeg'):
        spec = spec[:-5]
    elif spec.lower().endswith('.png'):
        spec = spec[:-4]
    parts = {}
    for token in spec.split('&'):
        if not token:
            continue
        if ':' not in token:
            continue
        k, v = token.split(':', 1)
        parts[k.strip().lower()] = v.strip()
    media_type = parts.get('tmdb', '').split(':', 1)
    if len(media_type) != 2:
        raise ValueError('expected tmdb:{type}:{id}')
    kind, tmdb_id = media_type[0], media_type[1]
    if kind not in ALLOWED_TYPES:
        raise ValueError('tmdb type must be movie or series')
    return kind, {k: v for k, v in {
        'tmdb_id': tmdb_id,
        'imdb_id': parts.get('imdb') or None,
        'tvdb_id': parts.get('tvdb') or None,
    }.items()}


def safe_key(*parts: str) -> str:
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def normalize_lang(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace('_', '-').split('-')[0].lower()


def same_title(a: str, b: str) -> bool:
    def n(s):
        return re.sub(r'[^a-z0-9]+', '', s.lower())
    return n(a) == n(b)


def allowed_image_url(url: str) -> bool:
    host = (urlparse(url).hostname or '').lower()
    return host in {
        'image.tmdb.org',
        'theposterdb.com',
        'images.metahub.space',
        'artworks.thetvdb.com',
        'www.thetvdb.com',
        'cdn4.thetvdb.com',
        'tver.jp',
    } or host.endswith('.thetvdb.com')
