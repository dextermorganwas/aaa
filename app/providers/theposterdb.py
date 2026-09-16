import asyncio
import re
from html import unescape
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from ..models import Candidate, Lookup
from ..util import same_title


class ThePosterDBProvider:
    name = 'theposterdb'
    BASE = 'https://theposterdb.com'
    SEARCH = BASE + '/search'
    ASSET = BASE + '/api/assets/'

    def __init__(self, client, settings, limiter):
        self.client = client
        self.settings = settings
        self.limiter = limiter

    async def _get(self, url):
        async with self.limiter:
            response = await self.client.get(
                url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (compatible; StremioArtProxy/2.0)',
                    'Accept': 'text/html,application/xhtml+xml',
                },
            )
        response.raise_for_status()
        return response.text

    def _section(self, media_type: str) -> str:
        return 'shows' if media_type == 'series' else 'movies'

    def _build_search_urls(self, title: str, lookup: Lookup, year: int | None) -> list[str]:
        section = self._section(lookup.media_type)
        urls: list[str] = []
        seen: set[str] = set()

        def add(params: dict[str, str]) -> None:
            url = f'{self.SEARCH}?{urlencode(params)}'
            if url not in seen:
                seen.add(url)
                urls.append(url)

        # First ask TPDb using all known identifiers at once. TPDb's search UI exposes
        # dedicated IMDb/TMDb/TVDb fields; keeping the identifiers in the query makes this
        # much more reliable than title-only matching when TPDb has multiple same-name titles.
        params = {'section': section}
        if title:
            params['term'] = title
        if year:
            params['year'] = str(year)
        if lookup.imdb_id:
            params['imdb_id'] = lookup.imdb_id
        if lookup.tmdb_id:
            params['tmdb_id'] = lookup.tmdb_id
        if lookup.tvdb_id:
            params['tvdb_id'] = lookup.tvdb_id
        if len(params) > 1:
            add(params)

        # Search each external ID as the term as a fallback. Older/newer TPDb search
        # implementations have differed in how the dedicated ID fields are handled.
        for key, value in (
            ('tmdb', lookup.tmdb_id),
            ('imdb', lookup.imdb_id),
            ('tvdb', lookup.tvdb_id),
        ):
            if value:
                # Search the identifier itself and also use TPDb's dedicated search field. The
                # latter is important because TPDb exposes separate IMDb/TMDb/TVDb search inputs,
                # while older pages did not always index IDs as ordinary search terms.
                add({'section': section, 'term': str(value)})
                add({'section': section, f'{key}_id': str(value)})
                add({'section': section, key: str(value)})

        # Finally do a clean title/year search, which mirrors the working strategy from the
        # uploaded TPDb Plex picker but restricts it to the correct media section.
        if title:
            title_params = {'section': section, 'term': title}
            if year:
                title_params['year'] = str(year)
            add(title_params)
            add({'section': section, 'term': title})

        return urls

    @staticmethod
    def _extract_year(text: str) -> int | None:
        m = re.search(r'\((\d{4})\)', text or '')
        return int(m.group(1)) if m else None

    @staticmethod
    def _clean_search_title(text: str) -> str:
        text = unescape(text or '')
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _parse_search_targets(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, 'html.parser')
        targets: list[dict] = []
        seen: set[str] = set()

        # The uploaded TPDb picker uses /posters/<id> as the set target. Prefer those
        # links and their visible title/year rather than arbitrary data-poster-id values.
        for anchor in soup.find_all('a', href=re.compile(r'/(?:posters|poster)/\d+')):
            href = anchor.get('href') or ''
            match = re.search(r'/(?:posters|poster)/(\d+)', href)
            if not match:
                continue
            target_id = match.group(1)
            absolute = urljoin(self.BASE, href)
            if absolute in seen:
                continue
            title = self._clean_search_title(anchor.get_text(' ', strip=True))
            # Some markup puts the human-readable title in an attribute instead of anchor text.
            if not title:
                title = self._clean_search_title(anchor.get('title') or anchor.get('aria-label') or '')
            if not title:
                image = anchor.find('img')
                title = self._clean_search_title(image.get('alt') if image else '')
            if not title:
                continue
            seen.add(absolute)
            targets.append({
                'id': target_id,
                'url': absolute,
                'title': title,
                'year': self._extract_year(title),
            })
        return targets

    async def _search_page_targets(self, url: str) -> list[dict]:
        html = await self._get(url)
        targets = self._parse_search_targets(html)
        # TPDb exposes rel="next" on search pagination. Follow a small configurable number of pages.
        max_pages = max(1, min(int(getattr(self.settings, 'tpdb_search_pages', 2)), 8))
        page_count = 1
        next_link = BeautifulSoup(html, 'html.parser').find('a', rel=lambda value: value and 'next' in value)
        current = url
        while next_link is not None and page_count < max_pages:
            href = next_link.get('href')
            if not href:
                break
            current = urljoin(self.BASE, href)
            page_html = await self._get(current)
            targets.extend(self._parse_search_targets(page_html))
            page_count += 1
            next_link = BeautifulSoup(page_html, 'html.parser').find('a', rel=lambda value: value and 'next' in value)

        deduped = []
        seen = set()
        for target in targets:
            if target['url'] in seen:
                continue
            seen.add(target['url'])
            deduped.append(target)
        return deduped

    @staticmethod
    def _page_ids(html: str) -> dict[str, set[str]]:
        lower = html.lower()
        ids = {'tmdb': set(), 'imdb': set(), 'tvdb': set()}

        # Link-based matching is stronger than arbitrary number matching.
        for match in re.finditer(r'https?://(?:www\.)?themoviedb\.org/(?:movie|tv)/(\d+)', html, re.I):
            ids['tmdb'].add(match.group(1))
        for match in re.finditer(r'https?://(?:www\.)?imdb\.com/title/(tt\d+)', html, re.I):
            ids['imdb'].add(match.group(1).lower())
        for match in re.finditer(r'https?://(?:www\.)?thetvdb\.com/[^\"\']*/?(\d{3,})', html, re.I):
            ids['tvdb'].add(match.group(1))

        # Also inspect nearby text around labelled IDs, since TPDb has exposed these as search/detail
        # fields even when the page doesn't render them as clickable links.
        patterns = {
            'tmdb': r'(?:tmdb|themoviedb)[^0-9]{0,60}(\d{2,})',
            'imdb': r'(?:imdb)[^t0-9]{0,60}(tt\d+)',
            'tvdb': r'(?:tvdb|thetvdb)[^0-9]{0,60}(\d{3,})',
        }
        for kind, pattern in patterns.items():
            for match in re.finditer(pattern, lower, re.I):
                value = match.group(1)
                ids[kind].add(value.lower() if kind == 'imdb' else value)
        return ids

    @staticmethod
    def _target_matches_title(target: dict, title: str, year: int | None) -> tuple[bool, bool]:
        target_title = target.get('title') or ''
        base = re.sub(r'\s+\(\d{4}\)\s*$', '', target_title).strip()
        if not same_title(title, base):
            return False, False
        target_year = target.get('year')
        return True, year is not None and target_year == year

    def _target_rank(self, target: dict, title: str, year: int | None, lookup: Lookup, page_html: str) -> tuple[int, int, int]:
        title_ok, year_ok = self._target_matches_title(target, title, year)
        ids = self._page_ids(page_html)
        id_matches = int(bool(lookup.tmdb_id and lookup.tmdb_id in ids['tmdb'])) + int(bool(lookup.imdb_id and lookup.imdb_id.lower() in ids['imdb'])) + int(bool(lookup.tvdb_id and lookup.tvdb_id in ids['tvdb']))
        return (id_matches, int(title_ok), int(year_ok))

    @staticmethod
    def _set_title(soup: BeautifulSoup, fallback: str) -> str:
        node = soup.find('p', id='set-title')
        if node:
            text = node.get_text(' ', strip=True)
            if text:
                return text
        node = soup.find('h1')
        if node:
            text = node.get_text(' ', strip=True)
            if text:
                return text
        return fallback

    @staticmethod
    def _extract_language(card) -> str | None:
        for element in [card, *card.find_all(True)]:
            for key, value in element.attrs.items():
                if 'lang' in key.lower():
                    value_text = ' '.join(value) if isinstance(value, list) else str(value)
                    if value_text.lower() in {'en', 'en-us', 'english'} or 'english' in value_text.lower():
                        return 'en'
                    if value_text:
                        return value_text.lower()
        return None

    @staticmethod
    def _extract_variation(card) -> str | None:
        # Keep this deliberately conservative. TPDb markup has changed over time; only honour an
        # explicit variation value rather than guessing from the poster itself.
        values = []
        for element in [card, *card.find_all(True)]:
            for key, value in element.attrs.items():
                if 'variation' in key.lower() or 'variant' in key.lower():
                    values.append(' '.join(value) if isinstance(value, list) else str(value))
            text = element.get_text(' ', strip=True)
            if re.search(r'\bvariation\b|\bvariant\b', text, re.I):
                values.append(text)
        for value in values:
            m = re.search(r'(?:variation|variant)\s*[:=-]\s*([\w -]+)', value, re.I)
            if m:
                return m.group(1).strip().lower()
        return None

    def _parse_set_artwork(self, html: str, lookup: Lookup, title: str, target: dict, identity_verified: bool = False) -> list[Candidate]:
        soup = BeautifulSoup(html, 'html.parser')
        grids = soup.find_all('div', class_='row')
        grid = None
        for candidate_grid in grids:
            classes = ' '.join(candidate_grid.get('class', []))
            if 'd-flex' in classes and 'flex-wrap' in classes and candidate_grid.find('div', class_='overlay'):
                grid = candidate_grid
                break
        if grid is None:
            # Fallback to the exact card selector used in the uploaded Artwork Uploader scraper.
            cards = soup.find_all('div', class_='col-6 col-lg-2 p-1')
        else:
            cards = grid.find_all('div', class_='col-6 col-lg-2 p-1')

        result: list[Candidate] = []
        for card in cards:
            media_anchor = card.find('a', class_='text-white', attrs={'data-toggle': 'tooltip'})
            media_type = (media_anchor.get('title') if media_anchor else '') or ''
            media_type = media_type.strip().lower()
            overlay = card.find('div', class_='overlay')
            poster_id = overlay.get('data-poster-id') if overlay else None
            if not poster_id or not str(poster_id).isdigit():
                continue
            poster_title_node = card.find('p', class_='p-0 mb-1 text-break')
            poster_title = poster_title_node.get_text(' ', strip=True) if poster_title_node else ''
            base_title = re.sub(r'\s+\(\d{4}\)', '', poster_title).strip()

            language = self._extract_language(card)
            variation = self._extract_variation(card)

            # The uploader project shows that the main set distinguishes Movie vs Show and that
            # series cover art is the "Show" entry whose parsed season is "Cover".
            if lookup.media_type == 'movie':
                if media_type and media_type != 'movie':
                    continue
                if poster_title and title and not same_title(title, base_title) and not identity_verified:
                    continue
                kind = 'poster'
            else:
                if media_type != 'show':
                    continue
                season_match = re.search(r'\s+-\s+Season\s+\d+\s*$', poster_title, re.I)
                specials_match = re.search(r'\s+-\s+Specials\s*$', poster_title, re.I)
                if season_match or specials_match:
                    continue
                if poster_title and title and not same_title(title, base_title) and not identity_verified:
                    continue
                kind = 'poster'

            # Prefer explicit English metadata if the page provides it. If TPDb omits language
            # metadata, keep the candidate: otherwise an otherwise-valid TPDb set would disappear
            # simply because the current markup didn't expose the field.
            if language and language not in {'en', 'en-us', 'english'}:
                continue

            # Only reject an explicit non-original variation; if the field is not exposed, retain it.
            if variation and variation not in {'original', 'default'}:
                continue

            candidate = Candidate(
                provider=self.name,
                url=self.ASSET + str(poster_id),
                language='en' if language in {'en', 'en-us', 'english'} else language,
                kind=kind,
                label='TPDb • English • Show Cover' if lookup.media_type == 'series' else 'TPDb • English',
                permanent=True,
                metadata={
                    'poster_id': str(poster_id),
                    'set_url': target['url'],
                    'set_title': self._set_title(soup, target.get('title', '')),
                    'artwork_title': poster_title,
                    'media_type': media_type,
                    'season': 'Cover' if lookup.media_type == 'series' else None,
                    'language_status': language or 'unknown',
                    'variation_status': variation or 'unknown',
                    'lookup_ids': {
                        'tmdb': lookup.tmdb_id,
                        'imdb': lookup.imdb_id,
                        'tvdb': lookup.tvdb_id,
                    },
                },
            )
            result.append(candidate)
        return result

    async def _target_candidates(self, target: dict, lookup: Lookup, title: str, year: int | None) -> tuple[dict, list[Candidate]]:
        page_html = await self._get(target['url'])
        title_ok, year_ok = self._target_matches_title(target, title, year)
        page_ids = self._page_ids(page_html)
        id_match_count = sum(
            bool(value and value in page_ids[kind])
            for kind, value in (
                ('tmdb', lookup.tmdb_id),
                ('imdb', lookup.imdb_id.lower() if lookup.imdb_id else None),
                ('tvdb', lookup.tvdb_id),
            )
        )
        # Do not accept an unrelated TPDb result. Identifier matches are strongest; when TPDb does
        # not expose IDs on the page, exact title + year (or exact title when year is unavailable) is
        # the safe fallback.
        if id_match_count == 0 and not title_ok:
            return target, []
        if year is not None and target.get('year') not in (None, year) and id_match_count == 0:
            return target, []

        candidates = self._parse_set_artwork(page_html, lookup, title, target, identity_verified=(id_match_count > 0))
        if not candidates:
            return target, []

        # If explicit language metadata exists, English has already been enforced. Sort only by
        # the order returned in the TPDb HTML; the first matching artwork wins as requested.
        return target, candidates

    async def candidates(self, title: str, year: int | None, media_type: str, lookup: Lookup | None = None) -> list[Candidate]:
        if not title and not lookup:
            return []
        lookup = lookup or Lookup(media_type, None, None, None)
        if not title:
            title = ''

        urls = self._build_search_urls(title, lookup, year)
        if not urls:
            return []

        search_results = await asyncio.gather(
            *(self._search_page_targets(url) for url in urls),
            return_exceptions=True,
        )
        targets: list[dict] = []
        seen = set()
        for result in search_results:
            if isinstance(result, Exception):
                continue
            for target in result:
                if target['url'] not in seen:
                    seen.add(target['url'])
                    targets.append(target)

        # Rank before fetching set pages: exact title/year targets first, then identifier-search
        # results, while keeping TPDb's own ordering among otherwise-equal candidates.
        indexed_targets = list(enumerate(targets))

        def preliminary_rank(pair: tuple[int, dict]) -> tuple[int, int, int, int]:
            index, target = pair
            title_ok, year_ok = self._target_matches_title(target, title, year)
            exact_target_term = any(
                value and value in (target.get('title') or '')
                for value in (lookup.tmdb_id, lookup.imdb_id, lookup.tvdb_id)
            )
            return (int(title_ok), int(year_ok), int(exact_target_term), -index)

        indexed_targets.sort(key=preliminary_rank, reverse=True)
        targets = [target for _, target in indexed_targets]

        max_targets = max(1, int(getattr(self.settings, 'tpdb_max_targets', 8)))
        for target in targets[:max_targets]:
            try:
                _, candidates = await self._target_candidates(target, lookup, title, year)
            except Exception:
                continue
            if candidates:
                return candidates
        return []
