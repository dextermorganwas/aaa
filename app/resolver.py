import asyncio
import hashlib
import mimetypes
from datetime import datetime, timezone, timedelta
from pathlib import Path
import httpx

from .models import Lookup, Candidate, Resolution
from .providers.tmdb import TMDBProvider
from .providers.tvdb import TVDBProvider
from .providers.metahub import MetaHubProvider
from .providers.theposterdb import ThePosterDBProvider
from .util import safe_key, normalize_lang, allowed_image_url


class Resolver:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.client = httpx.AsyncClient(follow_redirects=True, timeout=settings.http_timeout_seconds)
        self.limiter = asyncio.Semaphore(settings.max_concurrent_provider_requests)
        self.tmdb = TMDBProvider(self.client, settings, self.limiter)
        self.tvdb = TVDBProvider(self.client, settings, self.limiter)
        self.metahub = MetaHubProvider()
        self.tpdb = ThePosterDBProvider(self.client, settings, self.limiter)
        self.singleflight = {}
        self.background_tasks = set()
        self.background_keys = set()
        self.shutdown_event = asyncio.Event()

    async def close(self):
        self.shutdown_event.set()
        if self.background_tasks:
            await asyncio.gather(*list(self.background_tasks), return_exceptions=True)
        await self.client.aclose()

    def _track(self, key, coro):
        if key in self.background_keys:
            return
        self.background_keys.add(key)
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)

        def done(t):
            self.background_tasks.discard(t)
            self.background_keys.discard(key)

        task.add_done_callback(done)

    async def resolve(self, lookup: Lookup, art_type: str):
        key = safe_key(lookup.media_type, lookup.tmdb_id, lookup.imdb_id, lookup.tvdb_id, art_type)
        current = self.singleflight.get(key)
        if current:
            return await current
        task = asyncio.create_task(self._resolve(key, lookup, art_type))
        self.singleflight[key] = task
        try:
            return await task
        finally:
            self.singleflight.pop(key, None)

    async def _resolve(self, key, lookup, art_type):
        item = await self.db.upsert_item(lookup)
        selection = await self.db.get_selection(item['id'], art_type)
        # TPDb matches are revalidated when its matching algorithm version changes. This also
        # clears old cached selections produced by the buggy matcher.
        if selection and selection['provider'] == 'theposterdb' and int(selection['resolver_version'] or 0) < self.settings.tpdb_matcher_version:
            selection = None
        if selection and selection['local_path'] and (selection['is_override'] or self._valid_selection(selection)):
            return Resolution(
                lookup.media_type,
                art_type,
                Candidate(selection['provider'], selection['source_url'] or '', kind=art_type),
                item['title'],
                item['original_language'],
                selection['source_url'],
                selection['content_type'] or 'image/jpeg',
            )

        title = item['title']
        original_lang = item['original_language']
        release_year = item['release_year'] if 'release_year' in item.keys() else None
        if not title or not original_lang or not release_year:
            try:
                data, lookup = await self.tmdb.metadata(lookup)
                title = title or data.get('title') or data.get('name')
                original_lang = original_lang or normalize_lang(data.get('original_language'))
                release_date = data.get('release_date') or data.get('first_air_date') or ''
                release_year = release_year or (int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None)
                item = await self.db.upsert_item(lookup, title, original_lang, release_year)
            except Exception as exc:
                await self.db.record_failure('tmdb', key, str(exc))

        # TPDb is special: keep the user-facing wait short. If lookup OR asset download does not
        # finish within the configured fast timeout, continue the normal provider chain and finish
        # TPDb in the background for the next request.
        if art_type == 'poster' and title:
            tpdb_key = f"tpdb:{key}"
            try:
                async with asyncio.timeout(self.settings.tpdb_timeout_seconds):
                    tp_candidates = await self.tpdb.candidates(title, release_year, lookup.media_type, lookup)
                    if tp_candidates:
                        hit = await self._materialize(item['id'], art_type, tp_candidates[0], key, replace_if_source_changed=True)
                        if hit:
                            await self.db.save_candidates(item['id'], art_type, tp_candidates)
                            return hit
            except Exception as exc:
                await self.db.record_failure('theposterdb', key, str(exc))

            if self.settings.tpdb_background_refresh:
                self._track(tpdb_key, self._background_tpdb(item['id'], lookup, title, release_year, key, art_type))

        chain = []
        if art_type in {'poster', 'backdrop', 'logo'}:
            chain.append(('tmdb_en', lambda: self.tmdb.candidates(lookup, art_type, 'en', art_type == 'backdrop')))
            chain.append(('tvdb_en', lambda: self.tvdb.candidates(lookup, art_type, 'en', art_type == 'backdrop')))
            if original_lang and original_lang != 'en':
                chain.append(('tmdb_original', lambda: self.tmdb.candidates(lookup, art_type, original_lang, art_type == 'backdrop')))
                chain.append(('tvdb_original', lambda: self.tvdb.candidates(lookup, art_type, original_lang, art_type == 'backdrop')))
            if lookup.imdb_id:
                chain.append(('metahub', lambda: asyncio.sleep(0, result=self.metahub.candidates(lookup.imdb_id, art_type))))
            chain.append(('tmdb_primary', lambda: self.tmdb.primary(lookup, art_type)))
            chain.append(('tvdb_primary', lambda: self.tvdb.primary(lookup, art_type)))

        for provider, fn in chain:
            if await self.db.is_failure_cooldown(provider, key, self.settings.failure_cooldown_seconds):
                continue
            try:
                result = await self._with_retries(fn)
                arr = result if isinstance(result, list) else ([result] if result else [])
                if arr:
                    await self.db.save_candidates(item['id'], art_type, arr)
                    hit = await self._materialize(item['id'], art_type, arr[0], key, replace_if_source_changed=True)
                    if hit:
                        return hit
            except Exception as exc:
                await self.db.record_failure(provider, key, str(exc))

        raise LookupError('no artwork found')

    async def _background_tpdb(self, item_id, lookup, title, year, key, art_type):
        try:
            candidates = await self.tpdb.candidates(title, year, lookup.media_type, lookup)
            if not candidates:
                return
            await self.db.save_candidates(item_id, art_type, candidates)
            selection = await self.db.get_selection(item_id, art_type)
            if selection and selection['is_override']:
                return
            hit = await self._materialize(item_id, art_type, candidates[0], key, replace_if_source_changed=True)
            if hit:
                await self.db.clear_failure('theposterdb', key)
        except Exception as exc:
            await self.db.record_failure('theposterdb', key, str(exc))

    async def _with_retries(self, fn):
        last = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                return await fn()
            except Exception as exc:
                last = exc
                if attempt >= self.settings.max_retries:
                    break
                await asyncio.sleep(self.settings.retry_backoff_seconds * (2 ** attempt))
        raise last

    def _valid_selection(self, row):
        if row['expires_at'] is None:
            return True
        try:
            return datetime.fromisoformat(row['expires_at']) > datetime.now(timezone.utc)
        except Exception:
            return False

    async def _materialize(self, item_id, art_type, candidate, key=None, replace_if_source_changed=False, allow_replace=False):
        if not allowed_image_url(candidate.url):
            return None
        path = self._path(item_id, art_type, candidate)
        existing = await self.db.get_selection(item_id, art_type)
        source_matches = bool(existing and existing['provider'] == candidate.provider and existing['source_url'] == candidate.url)
        try:
            if path.exists() and path.stat().st_size > 0 and (source_matches or not replace_if_source_changed) and not allow_replace:
                ctype = existing['content_type'] if existing else (mimetypes.guess_type(path.name)[0] or 'image/jpeg')
                await self.db.set_selection(item_id, art_type, candidate.provider, candidate.url, str(path), ctype, self._expiry(candidate), False, self.settings.tpdb_matcher_version)
                return Resolution('', art_type, candidate, fetched_url=candidate.url, content_type=ctype)

            path.parent.mkdir(parents=True, exist_ok=True)
            async with self.client.stream(
                'GET',
                candidate.url,
                headers={'User-Agent': 'Mozilla/5.0 (StremioArtProxy/3.0)', 'Accept': 'image/avif,image/webp,image/jpeg,image/png,*/*'},
                timeout=self.settings.http_timeout_seconds,
            ) as response:
                response.raise_for_status()
                total = 0
                tmp = path.with_suffix(path.suffix + '.part')
                with tmp.open('wb') as f:
                    async for chunk in response.aiter_bytes(65536):
                        total += len(chunk)
                        if total > self.settings.max_image_bytes:
                            raise ValueError('image exceeds MAX_IMAGE_BYTES')
                        f.write(chunk)
                tmp.replace(path)
                ctype = response.headers.get('content-type', '').split(';')[0] or mimetypes.guess_type(path.name)[0] or 'image/jpeg'

            await self.db.set_selection(item_id, art_type, candidate.provider, candidate.url, str(path), ctype, self._expiry(candidate), False, self.settings.tpdb_matcher_version)
            await self.db.clear_failure(candidate.provider, key or safe_key(item_id, art_type))
            return Resolution('', art_type, candidate, fetched_url=candidate.url, content_type=ctype)
        except Exception as exc:
            await self.db.record_failure(candidate.provider, key or safe_key(item_id, art_type), str(exc))
            try:
                path.with_suffix(path.suffix + '.part').unlink(missing_ok=True)
            except Exception:
                pass
            return None

    def _expiry(self, candidate):
        ttl = self.settings.tpdb_cache_ttl_seconds if candidate.provider == 'theposterdb' else self.settings.cache_ttl_seconds
        if ttl <= 0:
            return None
        return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()

    def _path(self, item_id, art_type, candidate):
        ext = '.jpg'
        if candidate.url.lower().endswith('.png') or (candidate.provider == 'tvdb' and 'logo' in (candidate.label or '').lower()):
            ext = '.png'
        digest = hashlib.sha256(f"{candidate.provider}|{candidate.url}".encode()).hexdigest()[:16]
        return self.settings.image_dir / str(item_id) / f"{art_type}-{digest}{ext}"

    async def refresh_candidates(self, item_id, art_type):
        item = await self.db.get_item(item_id)
        if not item:
            raise KeyError(item_id)
        lookup = Lookup(item['media_type'], item['tmdb_id'], item['imdb_id'], item['tvdb_id'])
        data = []
        title = item['title']
        try:
            if art_type == 'poster' and title:
                data += await self.tpdb.candidates(title, item['release_year'], item['media_type'], lookup)
        except Exception:
            pass
        if art_type in ('poster', 'backdrop', 'logo'):
            for lang, textless in [('en', art_type == 'backdrop'), (normalize_lang(item['original_language']), art_type == 'backdrop')]:
                if not lang:
                    continue
                try:
                    data += await self.tmdb.candidates(lookup, art_type, lang, textless)
                except Exception:
                    pass
                try:
                    data += await self.tvdb.candidates(lookup, art_type, lang, textless)
                except Exception:
                    pass
            if lookup.imdb_id:
                data += self.metahub.candidates(lookup.imdb_id, art_type)
            try:
                primary = await self.tmdb.primary(lookup, art_type)
                if primary:
                    data.append(primary)
            except Exception:
                pass
            try:
                primary = await self.tvdb.primary(lookup, art_type)
                if primary:
                    data.append(primary)
            except Exception:
                pass
        await self.db.save_candidates(item_id, art_type, data)
        return data

    async def override(self, item_id, art_type, candidate_url, provider='manual'):
        item = await self.db.get_item(item_id)
        if not item:
            raise KeyError(item_id)
        candidate = Candidate(provider=provider, url=candidate_url, kind=art_type, label='Manual override', permanent=True)
        if not allowed_image_url(candidate_url):
            raise ValueError('URL is not an allowed provider URL')
        hit = await self._materialize(item_id, art_type, candidate, allow_replace=True, replace_if_source_changed=True)
        if not hit:
            raise ValueError('could not download selected artwork')
        selection = await self.db.get_selection(item_id, art_type)
        await self.db.set_selection(item_id, art_type, provider, candidate_url, selection['local_path'], selection['content_type'], None, True, self.settings.tpdb_matcher_version)
        return selection
