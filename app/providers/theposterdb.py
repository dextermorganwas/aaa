import asyncio
import re
from html import unescape
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from ..models import Candidate, Lookup
from ..util import same_title


class ThePosterDBProvider:
    """Small, conservative TPDb resolver.

    TPDb's public search is title/category based; external-ID search is a Pro feature on
    the current public search page. We therefore use the public search exactly as a user
    would, then validate the actual poster-set page before accepting an asset.
    """

    name = "theposterdb"
    BASE = "https://theposterdb.com"
    SEARCH = BASE + "/search"
    ASSET = BASE + "/api/assets/"

    def __init__(self, client, settings, limiter):
        self.client = client
        self.settings = settings
        # Keep TPDb traffic deliberately serial. ThePosterDB is much happier with a single
        # outstanding crawl than the old implementation's burst of many parallel searches.
        self.limiter = limiter
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    async def _wait_rate_limit(self):
        interval = max(0.0, float(getattr(self.settings, "tpdb_request_interval_seconds", 1.0)))
        if interval <= 0:
            return
        async with self._request_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            delay = interval - (now - self._last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request = loop.time()

    async def _get(self, url):
        await self._wait_rate_limit()
        async with self.limiter:
            response = await self.client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; StremioArtProxy/3.0)",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
        response.raise_for_status()
        return response.text

    def _section(self, media_type: str) -> str:
        return "shows" if media_type == "series" else "movies"

    def _search_url(self, title: str, media_type: str, year: int | None) -> str:
        params = {"term": title, "section": self._section(media_type)}
        if year:
            params["year"] = str(year)
        return f"{self.SEARCH}?{urlencode(params)}"

    @staticmethod
    def _extract_year(text: str) -> int | None:
        match = re.search(r"\((\d{4})\)", text or "")
        return int(match.group(1)) if match else None

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", unescape(text or "")).strip()

    def _parse_search_targets(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        targets = []
        seen = set()
        # This is the same stable set-link pattern used by the uploaded TPDb apps.
        for anchor in soup.find_all("a", href=re.compile(r"/(?:posters|poster)/\d+")):
            href = anchor.get("href") or ""
            match = re.search(r"/(?:posters|poster)/(\d+)", href)
            if not match:
                continue
            absolute = urljoin(self.BASE, href)
            if absolute in seen:
                continue
            title = self._clean(anchor.get_text(" ", strip=True))
            if not title:
                title = self._clean(anchor.get("title") or anchor.get("aria-label") or "")
            if not title:
                image = anchor.find("img")
                title = self._clean(image.get("alt") if image else "")
            if not title:
                continue
            seen.add(absolute)
            targets.append({
                "id": match.group(1),
                "url": absolute,
                "title": title,
                "year": self._extract_year(title),
            })
        return targets

    async def _search_targets(self, title: str, media_type: str, year: int | None) -> list[dict]:
        first_url = self._search_url(title, media_type, year)
        html = await self._get(first_url)
        targets = self._parse_search_targets(html)
        max_pages = max(1, min(int(getattr(self.settings, "tpdb_search_pages", 1)), 4))

        # Follow only the site's actual next-page link. Do not fan out into many equivalent
        # title/ID searches: besides being slow, that was the source of rate-limit trouble.
        current_html = html
        page = 1
        while page < max_pages:
            soup = BeautifulSoup(current_html, "html.parser")
            next_link = soup.find("a", rel=lambda value: value and "next" in value)
            href = next_link.get("href") if next_link else None
            if not href:
                break
            current_html = await self._get(urljoin(self.BASE, href))
            targets.extend(self._parse_search_targets(current_html))
            page += 1

        out = []
        seen = set()
        for target in targets:
            if target["url"] in seen:
                continue
            seen.add(target["url"])
            out.append(target)
        return out

    @staticmethod
    def _target_matches(target: dict, title: str, year: int | None) -> tuple[bool, bool]:
        target_title = re.sub(r"\s*\(\d{4}\)\s*$", "", target.get("title", "")).strip()
        title_ok = same_title(title, target_title)
        year_ok = year is not None and target.get("year") == year
        return title_ok, year_ok

    @staticmethod
    def _media_type(card) -> str:
        anchor = card.find("a", class_="text-white", attrs={"data-toggle": "tooltip"})
        return ((anchor.get("title") if anchor else "") or "").strip().lower()

    @staticmethod
    def _season_is_cover(text: str) -> bool:
        if re.search(r"\s+-\s+Season\s+\d+\s*$", text, re.I):
            return False
        if re.search(r"\s+-\s+Specials\s*$", text, re.I):
            return False
        # The set's primary show poster is the plain Show entry. The uploaded application
        # represents this as season == "Cover".
        return True

    def _parse_set_artwork(self, html: str, lookup: Lookup, title: str, target: dict) -> list[Candidate]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all("div", class_="col-6 col-lg-2 p-1")
        candidates = []

        for card in cards:
            media_type = self._media_type(card)
            overlay = card.find("div", class_="overlay")
            poster_id = overlay.get("data-poster-id") if overlay else None
            if not poster_id or not str(poster_id).isdigit():
                continue

            poster_title_node = card.find("p", class_="p-0 mb-1 text-break")
            poster_title = self._clean(poster_title_node.get_text(" ", strip=True) if poster_title_node else "")
            base_title = re.sub(r"\s*\(\d{4}\)\s*$", "", poster_title).strip()

            if lookup.media_type == "movie":
                if media_type != "movie":
                    continue
            else:
                if media_type != "show":
                    continue
                if not self._season_is_cover(poster_title):
                    continue

            # Never rely on the image at the top of the set page. Only overlay poster IDs inside
            # the actual artwork grid are accepted. Title matching prevents unrelated cards.
            if poster_title and title and not same_title(title, base_title):
                continue

            candidates.append(
                Candidate(
                    provider=self.name,
                    url=self.ASSET + str(poster_id),
                    language="en",
                    kind="poster",
                    label="TPDb • English • Show Cover" if lookup.media_type == "series" else "TPDb • English",
                    permanent=True,
                    metadata={
                        "poster_id": str(poster_id),
                        "set_url": target["url"],
                        "set_title": target.get("title", ""),
                        "artwork_title": poster_title,
                        "media_type": media_type,
                    },
                )
            )
        return candidates

    async def candidates(self, title: str, year: int | None, media_type: str, lookup: Lookup | None = None) -> list[Candidate]:
        if not title:
            return []
        lookup = lookup or Lookup(media_type, None, None, None)
        targets = await self._search_targets(title, media_type, year)

        exact = []
        for index, target in enumerate(targets):
            title_ok, year_ok = self._target_matches(target, title, year)
            if not title_ok:
                continue
            # Exact title + matching year goes first, but TPDb's own order is preserved among ties.
            exact.append((int(year_ok), -index, target))
        exact.sort(reverse=True)

        max_targets = max(1, int(getattr(self.settings, "tpdb_max_targets", 3)))
        for _, _, target in exact[:max_targets]:
            try:
                page_html = await self._get(target["url"])
            except Exception:
                continue
            candidates = self._parse_set_artwork(page_html, lookup, title, target)
            if candidates:
                return candidates
        return []
