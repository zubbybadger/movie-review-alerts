"""One check: scan every site, analyze new reviews, send alerts, save state.

Run with:  python -m bot.main
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
import yaml

from . import notify
from .analyzer import AnalysisError, Verdict, analyze
from .sources import Candidate, Movie, clean_url, fetch_entries, find_candidates, make_client, normalize

log = logging.getLogger("bot")

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "watchlist.yaml"
STATE = ROOT / "state" / "state.json"

# Keeps one run (and its cost) bounded; anything left over is picked up next run.
MAX_ANALYSES_PER_RUN = int(os.environ.get("MAX_ANALYSES_PER_RUN", "40"))
# Pages that keep failing to analyze are given up on after this many tries.
MAX_ATTEMPTS = 3
# How many sites are read at the same time.
SITE_WORKERS = 8
# More new reviews than this for one movie in one run are sent as a single digest.
DIGEST_THRESHOLD = 3


def load_watchlist(path: Path = WATCHLIST) -> tuple[list[Movie], list[str]]:
    data = yaml.safe_load(path.read_text()) or {}
    movies = [
        Movie(str(m["title"]), m.get("year"), tuple(str(a) for a in m.get("also_known_as") or []))
        for m in data.get("movies") or []
        if m.get("title")
    ]
    sites = [str(s).strip() for s in data.get("sites") or [] if str(s).strip()]
    return movies, sites


def load_state(path: Path = STATE) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"seen": {}, "reviews": {}}


def save_state(state: dict, path: Path = STATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def seen_key(cand: Candidate) -> str:
    return f"{cand.movie.key}|{cand.url}"


def tally(state: dict, movie: Movie) -> tuple[int, int]:
    reviews = state["reviews"].get(movie.key, [])
    fresh = sum(r["verdict"] == "fresh" for r in reviews)
    return fresh, len(reviews)


def tally_line(state: dict, movie: Movie) -> str:
    fresh, total = tally(state, movie)
    return f"Score so far: {fresh}/{total} fresh ({round(100 * fresh / total)}%)"


def byline(v: Verdict) -> str:
    who = ", ".join(x for x in (v.critic, v.outlet) if x) or "A critic"
    return f"{who} ({v.rating})" if v.rating else who


def is_duplicate(state: dict, movie: Movie, v: Verdict) -> bool:
    """Same critic + outlet already counted (e.g. a syndicated copy on another site)."""
    if not v.critic:
        return False
    for r in state["reviews"].get(movie.key, []):
        if normalize(r["critic"]) == normalize(v.critic) and normalize(r["outlet"]) == normalize(v.outlet):
            return True
    return False


def alert_single(http: httpx.Client, state: dict, movie: Movie, v: Verdict, url: str) -> None:
    fresh = v.verdict == "fresh"
    title = f"{'🍅 FRESH' if fresh else '🤢 ROTTEN'}: {movie.title}"
    body = f"{byline(v)}: {v.summary}"
    if v.confidence == "low":
        body += " (low confidence: mixed review or partial text)"
    body += f"\n{tally_line(state, movie)}"
    notify.send(http, title, body, url, tags="tomato" if fresh else "nauseated_face")


def alert_digest(http: httpx.Client, state: dict, movie: Movie, items: list[tuple[Verdict, str]]) -> None:
    n_fresh = sum(v.verdict == "fresh" for v, _ in items)
    title = f"{len(items)} new reviews: {movie.title} ({n_fresh} fresh, {len(items) - n_fresh} rotten)"
    lines = [f"{'🍅' if v.verdict == 'fresh' else '🤢'} {byline(v)}: {v.summary}" for v, _ in items]
    lines.append(tally_line(state, movie))
    notify.send(http, title, "\n".join(lines), tags="tomato")


def scan_sites(http: httpx.Client, sites: list[str], movies: list[Movie]) -> dict[str, list[Candidate] | Exception]:
    """Read many sites at once; returns each site's candidates, or the error it hit."""
    def one(site: str) -> list[Candidate] | Exception:
        try:
            return find_candidates(http, site, movies)
        except httpx.HTTPError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=SITE_WORKERS) as pool:
        return dict(zip(sites, pool.map(one, sites)))


def run() -> int:
    movies, sites = load_watchlist()
    if not movies or not sites:
        log.info("watchlist has no movies or no sites; nothing to do")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY is not set; add it in the repo's Actions secrets. Skipping this check.")
        return 0
    state = load_state()
    llm = anthropic.Anthropic()
    new_by_movie: dict[str, list[tuple[Verdict, str]]] = defaultdict(list)
    movie_by_key = {m.key: m for m in movies}
    analyses = 0
    errors = 0

    with make_client() as http:
        candidates: list[Candidate] = []
        for site, found in scan_sites(http, sites, movies).items():
            if isinstance(found, Exception):
                errors += 1
                log.warning("could not read %s: %s", site, found)
            else:
                log.info("%s: %d matching links", site, len(found))
                candidates.extend(found)

        try:
            for cand in candidates:
                key = seen_key(cand)
                entry = state["seen"].get(key)
                if entry and (entry["status"] != "error" or entry.get("attempts", 0) >= MAX_ATTEMPTS):
                    continue
                if analyses >= MAX_ANALYSES_PER_RUN:
                    log.info("hit the per-run analysis cap; the rest will be checked next run")
                    break
                analyses += 1
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                try:
                    v = analyze(llm, http, cand)
                except (AnalysisError, anthropic.APIError, httpx.HTTPError) as exc:
                    errors += 1
                    attempts = (entry or {}).get("attempts", 0) + 1
                    state["seen"][key] = {"status": "error", "attempts": attempts, "at": now, "error": str(exc)[:300]}
                    log.warning("analysis failed (%d/%d) for %s: %s", attempts, MAX_ATTEMPTS, cand.url, exc)
                    continue

                if not v.is_review:
                    state["seen"][key] = {"status": "not_review", "at": now}
                    log.info("not a review of %s: %s", cand.movie.label, cand.url)
                    continue
                if is_duplicate(state, cand.movie, v):
                    state["seen"][key] = {"status": "duplicate", "at": now}
                    continue

                state["seen"][key] = {"status": "review", "at": now, "verdict": v.verdict}
                state["reviews"].setdefault(cand.movie.key, []).append(
                    {"url": cand.url, "verdict": v.verdict, "critic": v.critic, "outlet": v.outlet,
                     "rating": v.rating, "summary": v.summary, "confidence": v.confidence, "at": now}
                )
                new_by_movie[cand.movie.key].append((v, cand.url))
                log.info("%s -> %s (%s)", cand.url, v.verdict, v.confidence)
        finally:
            save_state(state)

        for key, items in new_by_movie.items():
            movie = movie_by_key[key]
            if len(items) > DIGEST_THRESHOLD:
                alert_digest(http, state, movie, items)
            else:
                for v, url in items:
                    alert_single(http, state, movie, v, url)

    log.info("done: %d analyzed, %d new reviews, %d errors",
             analyses, sum(len(i) for i in new_by_movie.values()), errors)
    return 0


def run_test() -> int:
    """Analyze the newest item on the first readable site and send it as a TEST alert.

    Doesn't touch the watchlist or saved state; it just proves the whole chain works.
    """
    _, sites = load_watchlist()
    llm = anthropic.Anthropic()
    with make_client() as http:
        for site in sites:
            try:
                entries = fetch_entries(http, site)
            except httpx.HTTPError as exc:
                log.warning("could not read %s: %s", site, exc)
                continue
            for url, title, summary in entries[:5]:
                cand = Candidate(Movie("the film this page reviews"), clean_url(url), title, summary, site)
                try:
                    v = analyze(llm, http, cand)
                except (AnalysisError, anthropic.APIError, httpx.HTTPError) as exc:
                    log.warning("analysis failed for %s: %s", url, exc)
                    continue
                if not v.is_review:
                    continue
                fresh = v.verdict == "fresh"
                ok = notify.send(
                    http,
                    f"TEST {'🍅 FRESH' if fresh else '🤢 ROTTEN'}: {title[:80]}",
                    f"{byline(v)}: {v.summary}\n(This is a test alert. Real alerts are only for movies on your watchlist.)",
                    cand.url,
                    tags="tomato" if fresh else "nauseated_face",
                )
                log.info("test alert %s: %s -> %s", "sent" if ok else "NOT sent", url, v.verdict)
                return 0 if ok else 1
    log.error("couldn't find a review to test with")
    return 1


def run_probe() -> int:
    """Report which sites can be read and how many links each lists. Changes nothing."""
    _, sites = load_watchlist()
    rows = []
    with make_client() as http:
        def one(site: str) -> tuple[str, str, int]:
            try:
                return site, "ok", len(fetch_entries(http, site))
            except httpx.HTTPStatusError as exc:
                return site, f"HTTP {exc.response.status_code}", 0
            except httpx.HTTPError as exc:
                return site, type(exc).__name__, 0

        with ThreadPoolExecutor(max_workers=SITE_WORKERS) as pool:
            rows = list(pool.map(one, sites))
    for site, status, n in rows:
        flag = "OK" if status == "ok" and n else "PROBLEM"
        log.info("PROBE %s %s | %s | %d links", flag, site, status, n)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write("| Site | Status | Links |\n| --- | --- | --- |\n")
            fh.writelines(f"| {s} | {st} | {n} |\n" for s, st, n in rows)
    ok = sum(1 for _, st, n in rows if st == "ok" and n)
    log.info("PROBE DONE %d of %d sites readable", ok, len(rows))
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if "--probe" in sys.argv:
        sys.exit(run_probe())
    sys.exit(run_test() if "--test" in sys.argv else run())


if __name__ == "__main__":
    main()
