import json

import httpx
import pytest

from bot import main, notify, sources
from bot.analyzer import AnalysisError, Verdict
from bot.sources import Movie

FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Film</title>
<item><title>‘Dune: Part Three’ review – a sandy triumph</title><link>https://ex.com/dune-3-review</link>
<description>&lt;p&gt;Villeneuve returns.&lt;/p&gt;</description></item>
<item><title>Box office: Wicked holds on</title><link>https://ex.com/box-office</link><description>Numbers.</description></item>
<item><title>Up close with a director</title><link>https://ex.com/up-close</link><description>Interview.</description></item>
</channel></rss>"""

PAGE = """<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body>
<a href="/reviews/dune-part-three#comments">Dune: Part Three review</a>
<a href="https://twitter.com/share?u=dune-part-three">Share Dune: Part Three</a>
<a href="/reviews/other">Some other film</a>
</body></html>"""


def transport(routes):
    def handler(request):
        body, ctype = routes[str(request.url)]
        return httpx.Response(200, text=body, headers={"content-type": ctype})
    return httpx.MockTransport(handler)


def client(routes):
    return httpx.Client(transport=transport(routes), follow_redirects=True)


def test_mentions_whole_words_and_punctuation():
    dune = Movie("Dune: Part Three")
    assert sources.mentions(dune, "‘Dune – Part Three’ review")
    assert not sources.mentions(Movie("Up"), "Box office update")
    assert sources.mentions(Movie("Up"), "Up review: Pixar soars")
    assert sources.mentions(Movie("Amélie"), "Amelie at 25")
    assert sources.mentions(Movie("Fast & Furious"), "Fast and Furious review")


def test_feed_site():
    c = client({"https://ex.com/feed": (FEED, "application/rss+xml")})
    found = sources.find_candidates(c, "https://ex.com/feed", [Movie("Dune: Part Three"), Movie("Up")])
    urls = {(f.movie.title, f.url) for f in found}
    assert ("Dune: Part Three", "https://ex.com/dune-3-review") in urls
    assert ("Up", "https://ex.com/up-close") in urls  # Claude filters non-reviews later
    assert all("box-office" not in u for _, u in urls)


def test_page_site_uses_feed_and_links_and_skips_offsite():
    c = client({
        "https://ex.com/reviews": (PAGE, "text/html"),
        "https://ex.com/feed.xml": (FEED, "application/rss+xml"),
    })
    found = sources.find_candidates(c, "https://ex.com/reviews", [Movie("Dune: Part Three")])
    urls = sorted(f.url for f in found)
    assert urls == ["https://ex.com/dune-3-review", "https://ex.com/reviews/dune-part-three"]


def verdict(**kw):
    base = dict(is_review=True, verdict="fresh", confidence="high", critic="A. Critic",
                outlet="Example Times", rating="", summary="Gorgeous.", used_full_text=True)
    base.update(kw)
    return Verdict(**base)


@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    wl = tmp_path / "watchlist.yaml"
    wl.write_text("movies:\n  - title: Dune Part Three\n    year: 2026\nsites:\n  - https://ex.com/feed\n")
    state_path = tmp_path / "state.json"
    orig_load_wl, orig_load_state, orig_save = main.load_watchlist, main.load_state, main.save_state
    monkeypatch.setattr(main, "load_watchlist", lambda: orig_load_wl(wl))
    monkeypatch.setattr(main, "load_state", lambda: orig_load_state(state_path))
    monkeypatch.setattr(main, "save_state", lambda s: orig_save(s, state_path))
    monkeypatch.setattr(main, "make_client", lambda: client({"https://ex.com/feed": (FEED, "application/rss+xml")}))
    monkeypatch.setattr(main.anthropic, "Anthropic", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    sent = []
    monkeypatch.setattr(notify, "send", lambda http, title, body, url=None, tags="": sent.append((title, body, url)) or True)
    return state_path, sent


def test_run_alerts_once_and_tallies(bot_env, monkeypatch):
    state_path, sent = bot_env
    calls = []
    monkeypatch.setattr(main, "analyze", lambda llm, http, cand: calls.append(cand.url) or verdict())

    main.run()
    assert calls == ["https://ex.com/dune-3-review"]
    assert len(sent) == 1
    title, body, url = sent[0]
    assert title == "🍅 FRESH: Dune Part Three"
    assert "A. Critic, Example Times: Gorgeous." in body
    assert "1/1 fresh (100%)" in body
    assert url == "https://ex.com/dune-3-review"

    main.run()  # second run: nothing new
    assert len(calls) == 1 and len(sent) == 1
    state = json.loads(state_path.read_text())
    assert state["seen"]["dune part three|2026|https://ex.com/dune-3-review"]["status"] == "review"


def test_non_review_is_silent(bot_env, monkeypatch):
    _, sent = bot_env
    monkeypatch.setattr(main, "analyze", lambda *a: verdict(is_review=False))
    main.run()
    assert sent == []


def test_errors_retry_then_give_up(bot_env, monkeypatch):
    state_path, sent = bot_env
    calls = []

    def boom(llm, http, cand):
        calls.append(1)
        raise AnalysisError("nope")

    monkeypatch.setattr(main, "analyze", boom)
    for _ in range(main.MAX_ATTEMPTS + 2):
        main.run()
    assert len(calls) == main.MAX_ATTEMPTS
    assert sent == []


def test_digest_when_many(bot_env, monkeypatch):
    _, sent = bot_env
    feed = FEED.replace("</channel>", "".join(
        f"<item><title>Dune Part Three review {i}</title><link>https://ex.com/r{i}</link></item>" for i in range(4)
    ) + "</channel>")
    monkeypatch.setattr(main, "make_client", lambda: client({"https://ex.com/feed": (feed, "application/rss+xml")}))
    n = iter(range(100))
    monkeypatch.setattr(main, "analyze", lambda *a: verdict(critic=f"Critic {next(n)}",
                                                             verdict="rotten" if next(n) % 4 == 0 else "fresh"))
    main.run()
    assert len(sent) == 1
    assert sent[0][0].startswith("5 new reviews: Dune Part Three")


def test_syndicated_copy_not_double_counted(bot_env, monkeypatch):
    _, sent = bot_env
    feed = FEED.replace("</channel>", "<item><title>Dune Part Three review (reprint)</title>"
                                      "<link>https://ex.com/reprint</link></item></channel>")
    monkeypatch.setattr(main, "make_client", lambda: client({"https://ex.com/feed": (feed, "application/rss+xml")}))
    monkeypatch.setattr(main, "analyze", lambda *a: verdict())
    main.run()
    assert len(sent) == 1


def test_notify_without_channels_does_not_crash(monkeypatch):
    for k in ("NTFY_TOPIC", "TWILIO_ACCOUNT_SID"):
        monkeypatch.delenv(k, raising=False)
    assert notify.send(None, "t", "b") is False


def test_ntfy_request(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "secret-topic")
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), **json.loads(request.content))
        return httpx.Response(200)

    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert notify.send(c, "🍅 FRESH: Dune", "body", "https://ex.com/r") is True
    assert seen == {"url": "https://ntfy.sh/", "topic": "secret-topic", "title": "🍅 FRESH: Dune",
                    "message": "body", "click": "https://ex.com/r", "tags": ["movie_camera"]}


def test_test_mode_sends_one_alert_without_touching_state(bot_env, monkeypatch):
    state_path, sent = bot_env
    monkeypatch.setattr(main, "analyze", lambda *a: verdict(verdict="rotten"))
    assert main.run_test() == 0
    assert len(sent) == 1 and sent[0][0].startswith("TEST 🤢 ROTTEN")
    assert not state_path.exists()


def test_email(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    for k in ("NTFY_TOPIC", "TWILIO_ACCOUNT_SID"):
        monkeypatch.delenv(k, raising=False)
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = host
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def login(self, user, pw):
            sent["login"] = (user, pw)
        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    assert notify.send(None, "🍅 FRESH: Dune", "Great.", "https://ex.com/r") is True
    assert sent["login"] == ("me@gmail.com", "abcdefghijklmnop")
    assert sent["msg"]["To"] == "me@gmail.com" and sent["msg"]["Subject"] == "🍅 FRESH: Dune"
    assert "https://ex.com/r" in sent["msg"].get_content()


def test_aliases_match():
    m = Movie("The Only Living Pickpocket in New York", aliases=("El último carterista de Nueva York",))
    assert sources.mentions(m, "Crítica: El último carterista de Nueva York")
    assert sources.mentions(m, "The Only Living Pickpocket in New York review")
