from ..models import Candidate, Lookup
from ..util import normalize_lang

class TMDBProvider:
    name = 'tmdb'
    base = 'https://api.themoviedb.org/3'
    image_base = 'https://image.tmdb.org/t/p'

    def __init__(self, client, settings, limiter):
        self.client=client; self.settings=settings; self.limiter=limiter

    async def _get(self, path, params=None):
        if not self.settings.tmdb_bearer_token:
            raise RuntimeError('TMDB_BEARER_TOKEN is not configured')
        headers={'Authorization':f'Bearer {self.settings.tmdb_bearer_token}','accept':'application/json'}
        async with self.limiter:
            r=await self.client.get(self.base+path, params=params, headers=headers)
        r.raise_for_status(); return r.json()

    def _root(self, lookup):
        return '/movie/'+lookup.tmdb_id if lookup.media_type=='movie' else '/tv/'+lookup.tmdb_id

    async def metadata(self, lookup: Lookup):
        params={'language':'en-US','append_to_response':'external_ids'}
        data=await self._get(self._root(lookup), params=params)
        ext=data.get('external_ids') or {}
        if lookup.imdb_id is None:
            lookup = Lookup(lookup.media_type, lookup.tmdb_id, ext.get('imdb_id'), lookup.tvdb_id)
        if lookup.tvdb_id is None:
            lookup = Lookup(lookup.media_type, lookup.tmdb_id, lookup.imdb_id, ext.get('tvdb_id'))
        return data, lookup

    def _image_url(self, path, kind):
        size={'poster':self.settings.tmdb_poster_size,'backdrop':self.settings.tmdb_backdrop_size,'logo':self.settings.tmdb_logo_size}[kind]
        return f'{self.image_base}/{size}/{path.lstrip("/")}'

    async def candidates(self, lookup, art_type, language=None, textless=False):
        path=self._root(lookup)+'/images'
        params={'include_image_language':'en,null' if language=='en' else (f'{language},null' if language else 'en,null')}
        data=await self._get(path, params=params)
        raw=data.get({'poster':'posters','backdrop':'backdrops','logo':'logos'}[art_type], [])
        result=[]
        lang_norm=normalize_lang(language)
        for x in raw:
            xlang=normalize_lang(x.get('iso_639_1'))
            if language and xlang != lang_norm:
                continue
            if textless and (xlang is not None or x.get('vote_count') is None and False):
                # TMDB's language field is the useful proxy for textless. Null language is retained.
                if xlang is not None:
                    continue
            result.append(Candidate(provider=self.name, url=self._image_url(x['file_path'],art_type), language=xlang,
                                    kind=art_type, width=x.get('width'), height=x.get('height'),
                                    score=x.get('vote_average'), metadata=x))
        return result

    async def primary(self, lookup, art_type):
        params={'language':'en-US','append_to_response':'images','include_image_language':'en,null'}
        data=await self._get(self._root(lookup), params=params)
        field={'poster':'poster_path','backdrop':'backdrop_path','logo':'logo_path'}[art_type]
        path=data.get(field)
        if not path and art_type=='logo':
            imgs=(data.get('images') or {}).get('logos') or []
            path=imgs[0].get('file_path') if imgs else None
        if not path:
            return None
        return Candidate(provider=self.name,url=self._image_url(path,art_type),kind=art_type,label='TMDB primary',metadata=data)
