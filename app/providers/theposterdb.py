import re
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
from ..models import Candidate
from ..util import same_title

class ThePosterDBProvider:
    name='theposterdb'
    SEARCH='https://theposterdb.com/search?page=1&term='
    ASSET='https://theposterdb.com/api/assets/'

    def __init__(self, client, settings, limiter):
        self.client=client; self.settings=settings; self.limiter=limiter

    async def _get(self,url):
        async with self.limiter:
            r=await self.client.get(url,headers={'User-Agent':'Mozilla/5.0 (compatible; StremioArtProxy/1.0)'})
        r.raise_for_status(); return r.text

    async def candidates(self, title, year, media_type):
        if not title: return []
        html=await self._get(self.SEARCH+quote_plus(title))
        soup=BeautifulSoup(html,'html.parser')
        result=[]
        # Search result tiles and poster pages both expose a stable poster id in existing TPDb tooling.
        for tile in soup.select('[data-poster-id]'):
            pid=tile.get('data-poster-id')
            if not pid: continue
            raw=tile.get_text(' ',strip=True)
            href=tile.find_parent('a')
            href=href.get('href') if href else None
            if href and '/posters/' in href:
                page=await self._get('https://theposterdb.com'+href if href.startswith('/') else href)
                ps=BeautifulSoup(page,'html.parser')
                title_text=(ps.find(id='set-title') or ps.find('h1') or ps.find('title'))
                page_title=title_text.get_text(' ',strip=True) if title_text else raw
                if title and not same_title(title,page_title.split(' (')[0]):
                    continue
            result.append(Candidate(provider=self.name,url=self.ASSET+str(pid),language='en',kind='poster',label='TPDb',permanent=True,metadata={'poster_id':pid,'raw':raw,'href':href}))
        # Some TPDb responses show posters as direct links without data-poster-id on the search page.
        if not result:
            for a in soup.find_all('a',href=re.compile(r'/posters/\d+')):
                href=a.get('href')
                m=re.search(r'/posters/(\d+)',href)
                if not m: continue
                pid=m.group(1)
                raw=a.get_text(' ',strip=True)
                if title and raw and not same_title(title,raw.split(' (')[0]): continue
                result.append(Candidate(provider=self.name,url=self.ASSET+pid,language='en',kind='poster',label='TPDb',permanent=True,metadata={'poster_id':pid,'raw':raw,'href':href}))
        return result
