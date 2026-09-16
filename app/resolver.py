import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import mimetypes
import httpx
from .models import Lookup, Candidate, Resolution
from .providers.tmdb import TMDBProvider
from .providers.tvdb import TVDBProvider
from .providers.metahub import MetaHubProvider
from .providers.theposterdb import ThePosterDBProvider
from .util import safe_key, normalize_lang, allowed_image_url

class Resolver:
    def __init__(self, settings, db):
        self.settings=settings; self.db=db
        self.client=httpx.AsyncClient(follow_redirects=True, timeout=settings.http_timeout_seconds)
        self.limiter=asyncio.Semaphore(settings.max_concurrent_provider_requests)
        self.tmdb=TMDBProvider(self.client,settings,self.limiter)
        self.tvdb=TVDBProvider(self.client,settings,self.limiter)
        self.metahub=MetaHubProvider()
        self.tpdb=ThePosterDBProvider(self.client,settings,self.limiter)
        self.singleflight={}
        self.background_tasks=set()
        self.shutdown_event=asyncio.Event()

    async def close(self):
        self.shutdown_event.set()
        if self.background_tasks:
            await asyncio.gather(*list(self.background_tasks), return_exceptions=True)
        await self.client.aclose()

    def _track(self, coro):
        t=asyncio.create_task(coro); self.background_tasks.add(t); t.add_done_callback(self.background_tasks.discard); return t

    async def resolve(self, lookup: Lookup, art_type: str):
        key=safe_key(lookup.media_type,lookup.tmdb_id,lookup.imdb_id,lookup.tvdb_id,art_type)
        current=self.singleflight.get(key)
        if current: return await current
        task=asyncio.create_task(self._resolve(key,lookup,art_type))
        self.singleflight[key]=task
        try: return await task
        finally: self.singleflight.pop(key,None)

    async def _resolve(self,key,lookup,art_type):
        item=await self.db.upsert_item(lookup)
        selection=await self.db.get_selection(item['id'],art_type)
        if selection and selection['provider'] == 'theposterdb' and int(selection['resolver_version'] or 0) < self.settings.tpdb_matcher_version:
            selection = None
        if selection and selection['local_path'] and (selection['is_override'] or self._valid_selection(selection)):
            return Resolution(lookup.media_type,art_type,Candidate(selection['provider'],selection['source_url'] or '',kind=art_type),item['title'],item['original_language'],selection['source_url'],selection['content_type'] or 'image/jpeg')

        title=item['title']; original_lang=item['original_language']; release_year=item['release_year']
        if not title or not original_lang:
            try:
                data, lookup = await self.tmdb.metadata(lookup)
                title=title or data.get('title') or data.get('name')
                original_lang=original_lang or normalize_lang(data.get('original_language'))
                release_date=data.get('release_date') or data.get('first_air_date') or ''
                release_year = release_year or (int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None)
                item=await self.db.upsert_item(lookup,title,original_lang,release_year)
            except Exception as exc:
                await self.db.record_failure('tmdb',key,str(exc))

        candidates=[]
        # TPDb is intentionally attempted first with a small timeout. A timeout/failure does not block the rest.
        if art_type=='poster' and self.settings.tpdb_background_refresh:
            try:
                async with asyncio.timeout(self.settings.tpdb_timeout_seconds):
                    tp=self._tpdb_candidates(title, item and item['title'], lookup, art_type, release_year)
                    tp_candidates=await tp
                candidates.extend(tp_candidates)
            except Exception as exc:
                if self.settings.tpdb_background_refresh and title:
                    self._track(self._background_tpdb(item['id'],lookup,title,key,art_type))
                await self.db.record_failure('theposterdb',key,str(exc))
        elif art_type=='poster':
            try:
                async with asyncio.timeout(self.settings.tpdb_timeout_seconds):
                    candidates.extend(await self.tpdb.candidates(title, release_year, lookup.media_type, lookup))
            except Exception as exc:
                await self.db.record_failure('theposterdb',key,str(exc))

        if candidates:
            hit=await self._materialize(item['id'],art_type,candidates[0])
            if hit: return hit

        chain=[]
        if art_type in {'poster','backdrop','logo'}:
            chain.append(('tmdb_en',lambda: self.tmdb.candidates(lookup,art_type,'en',art_type=='backdrop')))
            chain.append(('tvdb_en',lambda: self.tvdb.candidates(lookup,art_type,'en',art_type=='backdrop')))
            if original_lang and original_lang!='en':
                chain.append(('tmdb_original',lambda: self.tmdb.candidates(lookup,art_type,original_lang,art_type=='backdrop')))
                chain.append(('tvdb_original',lambda: self.tvdb.candidates(lookup,art_type,original_lang,art_type=='backdrop')))
            if lookup.imdb_id:
                chain.append(('metahub',lambda: asyncio.sleep(0, result=self.metahub.candidates(lookup.imdb_id,art_type))))
            chain.append(('tmdb_primary',lambda: self.tmdb.primary(lookup,art_type)))
            chain.append(('tvdb_primary',lambda: self.tvdb.primary(lookup,art_type)))

        for provider,fn in chain:
            if await self.db.is_failure_cooldown(provider,key,self.settings.failure_cooldown_seconds):
                continue
            try:
                result=await self._with_retries(fn)
                arr=result if isinstance(result,list) else ([result] if result else [])
                if arr:
                    await self.db.save_candidates(item['id'],art_type,arr)
                    hit=await self._materialize(item['id'],art_type,arr[0],key)
                    if hit: return hit
            except Exception as exc:
                await self.db.record_failure(provider,key,str(exc))

        raise LookupError('no artwork found')

    async def _tpdb_candidates(self,title,_,lookup,art_type,year=None):
        return await self.tpdb.candidates(title, year, lookup.media_type, lookup)

    async def _background_tpdb(self,item_id,lookup,title,key,art_type):
        try:
            item = await self.db.get_item(item_id)
            year = item['release_year'] if item else None
            candidates=await self.tpdb.candidates(title, year, lookup.media_type, lookup)
            if not candidates: return
            await self.db.save_candidates(item_id,art_type,candidates)
            selection=await self.db.get_selection(item_id,art_type)
            if selection and selection['is_override']: return
            hit=await self._materialize(item_id,art_type,candidates[0], key, allow_replace=True)
            if hit:
                await self.db.clear_failure('theposterdb',key)
        except Exception as exc:
            await self.db.record_failure('theposterdb',key,str(exc))

    async def _with_retries(self, fn):
        last=None
        for attempt in range(self.settings.max_retries+1):
            try: return await fn()
            except Exception as exc:
                last=exc
                if attempt>=self.settings.max_retries: break
                await asyncio.sleep(self.settings.retry_backoff_seconds*(2**attempt))
        raise last

    def _valid_selection(self,row):
        if row['expires_at'] is None: return True
        try: return datetime.fromisoformat(row['expires_at']) > datetime.now(timezone.utc)
        except Exception: return False

    async def _materialize(self,item_id,art_type,candidate,key=None,allow_replace=False):
        if not allowed_image_url(candidate.url):
            return None
        path=self._path(item_id,art_type,candidate)
        try:
            if path.exists() and path.stat().st_size>0:
                ctype=mimetypes.guess_type(path.name)[0] or 'image/jpeg'
                await self.db.set_selection(item_id,art_type,candidate.provider,candidate.url,str(path),ctype,self._expiry(candidate),False,self.settings.tpdb_matcher_version)
                return Resolution('',art_type,candidate,fetched_url=candidate.url,content_type=ctype)
            async with self.client.stream('GET',candidate.url,headers={'User-Agent':'Mozilla/5.0 (StremioArtProxy)'},timeout=self.settings.http_timeout_seconds) as r:
                r.raise_for_status()
                total=0; path.parent.mkdir(parents=True,exist_ok=True)
                tmp=path.with_suffix(path.suffix+'.part')
                with tmp.open('wb') as f:
                    async for chunk in r.aiter_bytes(65536):
                        total += len(chunk)
                        if total>self.settings.max_image_bytes: raise ValueError('image exceeds MAX_IMAGE_BYTES')
                        f.write(chunk)
                tmp.replace(path)
                ctype=r.headers.get('content-type','').split(';')[0] or mimetypes.guess_type(path.name)[0] or 'image/jpeg'
            await self.db.set_selection(item_id,art_type,candidate.provider,candidate.url,str(path),ctype,self._expiry(candidate),False,self.settings.tpdb_matcher_version)
            await self.db.clear_failure(candidate.provider,safe_key(item_id,art_type))
            return Resolution('',art_type,candidate,fetched_url=candidate.url,content_type=ctype)
        except Exception as exc:
            await self.db.record_failure(candidate.provider,key or safe_key(item_id,art_type),str(exc))
            try:
                path.with_suffix(path.suffix+'.part').unlink(missing_ok=True)
            except Exception: pass
            return None

    def _expiry(self,c):
        ttl=self.settings.tpdb_cache_ttl_seconds if c.provider=='theposterdb' else self.settings.cache_ttl_seconds
        if ttl<=0: return None
        return (datetime.now(timezone.utc)+timedelta(seconds=ttl)).isoformat()

    def _path(self,item_id,art_type,candidate):
        import hashlib
        ext='.jpg'
        url=candidate.url.lower()
        if '.png' in url or (candidate.provider=='tvdb' and 'logo' in (candidate.label or '').lower()):
            ext='.png'
        source_key = hashlib.sha256(f'{candidate.provider}|{candidate.url}'.encode('utf-8')).hexdigest()[:20]
        return self.settings.image_dir / str(item_id) / f'{art_type}-{source_key}{ext}'

    async def metadata_for_admin(self,item_id):
        item=await self.db.get_item(item_id)
        return item

    async def refresh_candidates(self,item_id,art_type):
        item=await self.db.get_item(item_id)
        if not item: raise KeyError(item_id)
        lookup=Lookup(item['media_type'],item['tmdb_id'],item['imdb_id'],item['tvdb_id'])
        data=[]
        title=item['title']
        if art_type=='poster' and title:
            try:data += await self.tpdb.candidates(title, None, item['media_type'], lookup)
            except Exception: pass
        if art_type in ('poster','backdrop','logo'):
            for lang,textless in [('en',art_type=='backdrop'),(normalize_lang(item['original_language']),art_type=='backdrop')]:
                if not lang: continue
                try:data += await self.tmdb.candidates(lookup,art_type,lang,textless)
                except Exception: pass
                try:data += await self.tvdb.candidates(lookup,art_type,lang,textless)
                except Exception: pass
            if lookup.imdb_id:data += self.metahub.candidates(lookup.imdb_id,art_type)
            try:
                p=await self.tmdb.primary(lookup,art_type)
                if p:data.append(p)
            except Exception: pass
            try:
                p=await self.tvdb.primary(lookup,art_type)
                if p:data.append(p)
            except Exception: pass
        await self.db.save_candidates(item_id,art_type,data)
        return data

    async def override(self,item_id,art_type,candidate_url,provider='manual'):
        item=await self.db.get_item(item_id)
        if not item: raise KeyError(item_id)
        c=Candidate(provider=provider,url=candidate_url,kind=art_type,label='Manual override',permanent=True)
        if not allowed_image_url(candidate_url): raise ValueError('URL is not an allowed provider URL')
        hit=await self._materialize(item_id,art_type,c,allow_replace=True)
        if not hit: raise ValueError('could not download selected artwork')
        sel=await self.db.get_selection(item_id,art_type)
        await self.db.set_selection(item_id,art_type,provider,candidate_url,sel['local_path'],sel['content_type'],None,True,self.settings.tpdb_matcher_version)
        return sel
