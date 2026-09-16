import pytest

from app.models import Lookup
from app.providers.theposterdb import ThePosterDBProvider


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        return FakeResponse(self.pages[url])


class Settings:
    tpdb_search_pages = 1
    tpdb_max_targets = 8


async def _provider(pages):
    import asyncio
    return ThePosterDBProvider(FakeClient(pages), Settings(), asyncio.Semaphore(20))


@pytest.mark.asyncio
async def test_tpdb_selects_actual_set_and_not_arbitrary_search_tile():
    search = 'https://theposterdb.com/search?section=shows&term=Planet+Earth'
    identifier_search = 'https://theposterdb.com/search?section=shows&term=1044'
    id_search = 'https://theposterdb.com/search?section=shows&term=tt0795176'
    tvdb_search = 'https://theposterdb.com/search?section=shows&term=79257'
    combined = 'https://theposterdb.com/search?section=shows&term=Planet+Earth&imdb_id=tt0795176&tmdb_id=1044&tvdb_id=79257'
    set_url = 'https://theposterdb.com/posters/6792'

    search_html = '''
    <a href="/posters/6792">Planet Earth (2006)</a>
    <div class="bad-card" data-poster-id="114737">War on Everyone (2016)</div>
    '''
    set_html = '''
    <p id="set-title"><a>Planet Earth (2006)</a></p>
    <div class="row d-flex flex-wrap m-0 w-100">
      <div class="col-6 col-lg-2 p-1">
        <a class="text-white" data-toggle="tooltip" title="Show"></a>
        <div class="overlay" data-poster-id="222222"></div>
        <p class="p-0 mb-1 text-break">Planet Earth (2006)</p>
      </div>
      <div class="col-6 col-lg-2 p-1">
        <a class="text-white" data-toggle="tooltip" title="Show"></a>
        <div class="overlay" data-poster-id="333333"></div>
        <p class="p-0 mb-1 text-break">Planet Earth (2006) - Season 1</p>
      </div>
    </div>
    '''
    pages = {k: search_html for k in (search, identifier_search, id_search, tvdb_search, combined)}
    pages[set_url] = set_html
    provider = await _provider(pages)
    result = await provider.candidates(
        'Planet Earth', 2006, 'series', Lookup('series', '1044', 'tt0795176', '79257')
    )
    assert [c.metadata['poster_id'] for c in result] == ['222222']
    assert all(c.url != 'https://theposterdb.com/api/assets/114737' for c in result)


@pytest.mark.asyncio
async def test_tpdb_requires_exact_title_when_no_identifier_is_exposed():
    search = 'https://theposterdb.com/search?section=movies&term=The+Thing'
    other = 'https://theposterdb.com/search?section=movies&term=thing'
    set_wrong = 'https://theposterdb.com/posters/100'
    set_right = 'https://theposterdb.com/posters/200'
    search_html = '''
    <a href="/posters/100">Thing (2010)</a>
    <a href="/posters/200">The Thing (1982)</a>
    '''
    set_wrong_html = '''
    <p id="set-title"><a>Thing (2010)</a></p>
    <div class="row d-flex flex-wrap m-0 w-100">
      <div class="col-6 col-lg-2 p-1"><a class="text-white" data-toggle="tooltip" title="Movie"></a><div class="overlay" data-poster-id="10"></div><p class="p-0 mb-1 text-break">Thing (2010)</p></div>
    </div>
    '''
    set_right_html = '''
    <p id="set-title"><a>The Thing (1982)</a></p>
    <div class="row d-flex flex-wrap m-0 w-100">
      <div class="col-6 col-lg-2 p-1"><a class="text-white" data-toggle="tooltip" title="Movie"></a><div class="overlay" data-poster-id="20"></div><p class="p-0 mb-1 text-break">The Thing (1982)</p></div>
    </div>
    '''
    pages = {search: search_html, other: search_html, set_wrong: set_wrong_html, set_right: set_right_html}
    provider = await _provider(pages)
    result = await provider.candidates('The Thing', 1982, 'movie', Lookup('movie', '1091', None, None))
    assert [c.metadata['poster_id'] for c in result] == ['20']

@pytest.mark.asyncio
async def test_tpdb_accepts_identifier_verified_set_with_alternate_display_title():
    search = 'https://theposterdb.com/search?section=shows&tmdb_id=1044'
    set_url = 'https://theposterdb.com/posters/6792'
    search_html = '<a href="/posters/6792">Planet Earth II (2006)</a>'
    set_html = '''
    <p id="set-title"><a>Planet Earth II (2006)</a></p>
    <a href="https://www.themoviedb.org/tv/1044">TMDb</a>
    <div class="row d-flex flex-wrap m-0 w-100">
      <div class="col-6 col-lg-2 p-1">
        <a class="text-white" data-toggle="tooltip" title="Show"></a>
        <div class="overlay" data-poster-id="444444"></div>
        <p class="p-0 mb-1 text-break">Planet Earth II (2006)</p>
      </div>
    </div>
    '''
    pages = {search: search_html, set_url: set_html}
    provider = await _provider(pages)
    result = await provider.candidates('Planet Earth', 2006, 'series', Lookup('series', '1044', 'tt0795176', '79257'))
    assert [c.metadata['poster_id'] for c in result] == ['444444']
