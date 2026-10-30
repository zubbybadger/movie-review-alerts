"""Read a review and decide whether it's fresh or rotten, using Claude."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import anthropic
import httpx
import trafilatura

from .sources import Candidate

log = logging.getLogger(__name__)

MODEL = os.environ.get("REVIEW_MODEL") or "claude-opus-5-5"
# Reviews are short. This cap only guards against pages that extract badly
# (e.g. a whole comment section); we tell Claude when it was applied.
MAX_ARTICLE_CHARS = 60_000

SYSTEM = """You read film criticism and score it the way Rotten Tomatoes does.

A review is FRESH when the critic's overall take is positive: they would, on
balance, recommend the movie. It is ROTTEN when their overall take is negative
or they would not recommend it. Mixed reviews still get one verdict: decide
which way the critic leans overall, and lower your confidence.

When the critic gives a score, it decides the verdict the way Rotten Tomatoes
counts it, even if the prose sounds harsher or kinder: 2.5/4 stars or more,
3/5 or more, 6/10 or more, or a letter grade of B- or better is FRESH; anything
lower is ROTTEN. Look for the score in star graphics, headings and captions too.

You will get a page that may or may not be a review of the requested movie.
It could be news, an interview, a list, or a review of a different movie with
a similar title. Only set is_review to true when the page is a critic's
evaluation of that specific movie."""

SCHEMA = {
    "type": "object",
    "properties": {
        "is_review": {
            "type": "boolean",
            "description": "True only if this page is a critic's review of the requested movie.",
        },
        "verdict": {"type": "string", "enum": ["fresh", "rotten"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "critic": {"type": "string", "description": "Critic's name, or empty if unknown."},
        "outlet": {"type": "string", "description": "Publication name."},
        "rating": {
            "type": "string",
            "description": "The critic's own score if they gave one (e.g. '3/4', 'B+'), else empty.",
        },
        "summary": {
            "type": "string",
            "description": "One short sentence (under 25 words) on why the critic liked or disliked it.",
        },
    },
    "required": ["is_review", "verdict", "confidence", "critic", "outlet", "rating", "summary"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    is_review: bool
    verdict: str
    confidence: str
    critic: str
    outlet: str
    rating: str
    summary: str
    used_full_text: bool


class AnalysisError(Exception):
    pass


def fetch_article(client: httpx.Client, url: str) -> str | None:
    """Return the article's main text, or None if it can't be read (paywall, block, etc.)."""
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("could not fetch %s: %s", url, exc)
        return None
    text = trafilatura.extract(resp.text, url=str(resp.url), include_comments=False)
    return text if text and len(text) > 300 else None


def analyze(llm: anthropic.Anthropic, http: httpx.Client, cand: Candidate) -> Verdict:
    article = cand.inline_text or fetch_article(http, cand.url)
    used_full_text = article is not None
    if article is None:
        body = f"(The full page could not be loaded. Only the listing text is available.)\n\n{cand.summary}"
    elif len(article) > MAX_ARTICLE_CHARS:
        body = article[:MAX_ARTICLE_CHARS] + "\n\n(The page was cut off here because it was very long.)"
    else:
        body = article

    prompt = (
        f"Movie: {cand.movie.label}\n"
        f"Page URL: {cand.url}\n"
        f"Page headline: {cand.title}\n\n"
        f"<page>\n{body}\n</page>\n\n"
        "Is this a review of that movie, and if so, is it fresh or rotten?"
    )

    resp = llm.beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        extra_body={"fallbacks": "default"},
    )
    if resp.stop_reason == "refusal":
        raise AnalysisError(f"model declined to analyze {cand.url}")
    if resp.stop_reason == "max_tokens":
        raise AnalysisError(f"response cut off for {cand.url}")

    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnalysisError(f"unreadable response for {cand.url}: {exc}") from exc
    return Verdict(used_full_text=used_full_text, **data)
