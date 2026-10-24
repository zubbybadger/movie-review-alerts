# Movie review alerts

Watches the review sites you choose for new reviews of the movies you care
about. Each new review is read by Claude, which decides whether it's
**fresh** or **rotten**, and you get an alert on your phone with the verdict,
a one-line reason, and a running score.

```
🍅 FRESH: Dune: Part Three
Peter Debruge, Variety: Sweeping and gorgeous, if a touch long.
Score so far: 7/9 fresh (78%)
```

## How it works

Every 15 minutes GitHub runs the bot (`.github/workflows/check-reviews.yml`):

1. It reads `watchlist.yaml` for your movies and sites.
2. For each site it reads the RSS feed (found automatically) and the page
   itself, and picks out links that mention one of your movies.
3. For each link it hasn't seen before, it loads the article and asks Claude
   whether it's a review of that movie and whether it's fresh or rotten.
   News, interviews and lists are skipped quietly.
4. It sends the alert and records what it saw in `state/state.json`, so you
   never get the same review twice. If one movie gets more than 3 new reviews
   at once, you get a single summary instead of a flood.

## Changing what's watched

Edit `watchlist.yaml` on GitHub (open it, tap the pencil, save), or ask
Claude in the project thread to do it.

## Settings (GitHub → Settings → Secrets and variables → Actions)

| Secret | What it is |
| --- | --- |
| `ANTHROPIC_API_KEY` | Key from console.anthropic.com, used to analyze reviews. |
| `NTFY_TOPIC` | Your private ntfy topic name, for free push alerts. Treat it like a password. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `TWILIO_TO` | Optional, for real SMS via Twilio. |

Set the ntfy secret, the Twilio ones, or both. Optional repository
*variables*: `REVIEW_MODEL` (default `claude-opus-5-5`).

## Notes

- Some sites block automated readers or are paywalled. When the article
  can't be loaded, Claude judges from the headline and teaser and marks the
  verdict low confidence.
- GitHub pauses scheduled runs in a repo with no activity for 60 days. If
  alerts stop, open the Actions tab and re-enable the workflow.
- Run a check by hand: Actions → *Check for new reviews* → *Run workflow*.

## Developing

```
pip install -r requirements.txt pytest
python -m pytest
python -m bot.main   # one real check, using your environment's secrets
```
