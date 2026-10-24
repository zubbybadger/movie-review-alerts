"""Find candidate review links on the sites in the watchlist.

A site can be an RSS/Atom feed, a page that advertises a feed, or a plain
HTML page. In every case we end up with a list of Candidate links whose
title or summary mentions one of the watched movies.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urldefrag

import feedparser
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; movie-review-alerts/1.0; "
    "+https://github.com/movie-review-alerts)"
)
TIMEOUT = 20.0


@dataclass(frozen=True)
class Movie:
    title: str
    year: int | None = None
    # Other titles the movie goes by, e.g. its name on Spanish-language sites.
    aliases: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{normalize(self.title)}|{self.year or ''}"

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass
class Candidate:
    movie: Movie
    url: str
    title: str
    summary: str
    site: str
    # Set for sites that print their reviews right on the page instead of
    # linking to them: the stretch of page text around the movie's title.
    inline_text: str | None = None


# Marks the pseudo-entry for a page whose reviews are written on the page itself.
INLINE_PAGE = "\x00inline-page"
# A page with fewer article links than this is treated as a page of inline reviews.
INLINE_MIN_LINKS = 5
INLINE_WINDOW = 8000


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def mentions(movie: Movie, text: str) -> bool:
    """True when the movie's title (or an alias) appears in text as whole words."""
    hay = normalize(text)
    for name in (movie.title, *movie.aliases):
        needle = normalize(name)
        if needle and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay):
            return True
    return False


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
        timeout=TIMEOUT,
        follow_redirects=True,
    )


def clean_url(url: str) -> str:
    return urldefrag(url)[0]


def _looks_like_feed(resp: httpx.Response) -> bool:
    ctype = resp.headers.get("content-type", "")
    if any(t in ctype for t in ("rss", "atom", "xml")):
        return True
    head = resp.text[:500].lstrip().lower()
    return head.startswith("<?xml") or head.startswith("<rss") or "<feed" in head


def _feed_links(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for tag in soup.find_all("link", rel=lambda r: r and "alternate" in r):
        if any(t in (tag.get("type") or "") for t in ("rss", "atom")) and tag.get("href"):
            href = re.sub(r"^feed://", "https://", tag["href"].strip())
            # WordPress advertises a comments feed too; it never has reviews.
            if "/comments/feed" in href or "feed=comments" in href:
                continue
            links.append(urljoin(base, href))
    return links


def _get(client: httpx.Client, url: str) -> httpx.Response:
    """GET with one polite retry when a site says we're going too fast."""
    resp = client.get(url)
    if resp.status_code == 429:
        time.sleep(min(float(resp.headers.get("retry-after", "5") or 5), 15))
        resp = client.get(url)
    resp.raise_for_status()
    return resp


def _fallback_feeds(site: str) -> list[str]:
    """Common feed addresses to try when a site blocks its normal pages."""
    p = urlparse(site)
    root = f"{p.scheme}://{p.netloc}"
    path = p.path.rstrip("/")
    guesses = [f"{root}{path}/feed/"] if path else []
    guesses += [f"{root}/feed/", f"{root}/rss"]
    return list(dict.fromkeys(guesses))


def _entries_from_feed(xml: str) -> list[tuple[str, str, str]]:
    parsed = feedparser.parse(xml)
    out = []
    for e in parsed.entries:
        link = e.get("link")
        if not link:
            continue
        summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True)
        out.append((link, e.get("title", ""), summary))
    return out


def _entries_from_page(html: str, base: str) -> list[tuple[str, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base).netloc.removeprefix("www.")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = clean_url(urljoin(base, a["href"]))
        if not url.startswith("http") or url in seen:
            continue
        # Stay on the site itself; skip share buttons and ad links.
        if urlparse(url).netloc.removeprefix("www.") != host:
            continue
        text = a.get_text(" ", strip=True) or a.get("title", "") or a.get("aria-label", "")
        if text:
            seen.add(url)
            out.append((url, text, ""))
    return out


def fetch_entries(client: httpx.Client, site: str) -> list[tuple[str, str, str]]:
    """Return (url, title, summary) for every item listed on a site."""
    try:
        resp = _get(client, site)
    except httpx.HTTPStatusError as exc:
        # Many sites block page scrapers but still serve their RSS feed.
        for feed_url in _fallback_feeds(site):
            try:
                feed = _get(client, feed_url)
            except httpx.HTTPError:
                continue
            if _looks_like_feed(feed) and (entries := _entries_from_feed(feed.text)):
                log.info("%s blocked the page; using its feed %s", site, feed_url)
                return entries
        raise exc
    if _looks_like_feed(resp):
        return _entries_from_feed(resp.text)

    entries = []
    for feed_url in _feed_links(resp.text, str(resp.url))[:2]:
        try:
            feed = _get(client, feed_url)
            entries.extend(_entries_from_feed(feed.text))
        except httpx.HTTPError as exc:
            log.warning("feed %s failed: %s", feed_url, exc)
    # Feeds often hold only the latest ~20 items, so scan the page too.
    page_links = _entries_from_page(resp.text, str(resp.url))
    entries.extend(page_links)
    if len(page_links) < INLINE_MIN_LINKS:
        text = BeautifulSoup(resp.text, "html.parser").get_text("\n", strip=True)
        entries.append((str(resp.url), INLINE_PAGE, text))
    return entries


def _window(movie: Movie, text: str) -> str:
    """The text around the first mention of the movie (title or alias)."""
    lowered = text.lower()
    for name in (movie.title, *movie.aliases):
        i = lowered.find(name.lower())
        if i >= 0:
            return text[max(0, i - 500): i + INLINE_WINDOW]
    return text[:INLINE_WINDOW]


def find_candidates(client: httpx.Client, site: str, movies: list[Movie]) -> list[Candidate]:
    found: dict[tuple[str, str], Candidate] = {}
    entries = fetch_entries(client, site)
    log.info("%s: %d links listed", site, len(entries))
    for url, title, summary in entries:
        url = clean_url(url)
        for movie in movies:
            if title == INLINE_PAGE:
                # Only when no link on this site already points at the movie's review.
                if mentions(movie, summary) and not any(k == movie.key for k, _ in found):
                    found.setdefault((movie.key, url), Candidate(
                        movie, url, f"{movie.title} (review on {urlparse(url).netloc})", "", site,
                        inline_text=_window(movie, summary),
                    ))
            elif mentions(movie, title) or mentions(movie, summary):
                found.setdefault((movie.key, url), Candidate(movie, url, title, summary, site))
    return list(found.values())
