from ..models import Candidate, Lookup
from ..util import normalize_lang

class TVDBProvider:
    name='tvdb'
    base='https://api4.thetvdb.com/v4'

    def __init__(self, client, settings, limiter):
        self.client=client; self.settings=settings; self.limiter=limiter; self.token=None

    async def _login(self):
        if not self.settings.tvdb_api_key:
            raise RuntimeError('TVDB_API_KEY is not configured')
        payload={'apikey':self.settings.tvdb_api_key}
        if self.settings.tvdb_user_pin:
            payload['pin']=self.settings.tvdb_user_pin
        async with self.limiter:
            r=await self.client.post(self.base+'/login',json=payload)
        r.raise_for_status(); self.token=r.json()['data']['token']

    async def _get(self,path,params=None):
        if not self.token:
            await self._login()
        async with self.limiter:
            r=await self.client.get(self.base+path,params=params,headers={'Authorization':f'Bearer {self.token}','accept':'application/json'})
        if r.status_code==401:
            self.token=None; await self._login()
            async with self.limiter:
                r=await self.client.get(self.base+path,params=params,headers={'Authorization':f'Bearer {self.token}','accept':'application/json'})
        r.raise_for_status(); return r.json()

    async def metadata(self, lookup):
        if not lookup.tvdb_id:
            return {}, lookup
        path=f"/{'movies' if lookup.media_type=='movie' else 'series'}/{lookup.tvdb_id}/extended"
        data=(await self._get(path)).get('data') or {}
        remotes=data.get('remoteIds') or data.get('remote_ids') or []
        ids={str(x.get('sourceName','')).lower():x.get('id') for x in remotes if isinstance(x,dict)}
        imdb=lookup.imdb_id or ids.get('imdb')
        tmdb=lookup.tmdb_id or ids.get('tmdb')
        return data, Lookup(lookup.media_type, str(tmdb) if tmdb else lookup.tmdb_id, imdb, lookup.tvdb_id)

    async def candidates(self, lookup, art_type, language=None, textless=False):
        if not lookup.tvdb_id:
            return []
        if lookup.media_type == 'series':
            data=(await self._get(f"/series/{lookup.tvdb_id}/artworks")).get('data') or []
        else:
            ext=(await self._get(f"/movies/{lookup.tvdb_id}/extended")).get('data') or {}
            data=ext.get('artworks') or []
        wanted={'poster':{14},'backdrop':{15},'logo':{25}}[art_type]
        lang_norm=normalize_lang(language)
        out=[]
        for x in data:
            try: typ=int(x.get('type'))
            except (TypeError,ValueError): typ=None
            if typ not in wanted: continue
            xlang=normalize_lang(x.get('language'))
            if language and xlang != lang_norm: continue
            if textless and x.get('includesText') is True: continue
            url=x.get('image') or x.get('imageUrl')
            if not url: continue
            if isinstance(url,str) and url.startswith('/'):
                url='https://artworks.thetvdb.com'+url
            out.append(Candidate(provider=self.name,url=url,language=xlang,kind=art_type,width=x.get('width'),height=x.get('height'),label={14:'Poster',15:'Background',25:'ClearLogo'}.get(typ),score=x.get('score'),metadata=x))
        return out

    async def primary(self, lookup, art_type):
        if not lookup.tvdb_id: return None
        data=(await self._get(f"/{'movies' if lookup.media_type=='movie' else 'series'}/{lookup.tvdb_id}/extended")).get('data') or {}
        path=data.get('image')
        if path and isinstance(path,str):
            if path.startswith('/'): path='https://artworks.thetvdb.com'+path
            return Candidate(provider=self.name,url=path,kind=art_type,label='TVDB primary',metadata=data)
        return None

