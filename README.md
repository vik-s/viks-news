# viks-news

Personal daily semiconductor news feed. One scheduled job fetches RSS sources,
one Claude API call filters and summarizes, and the result is a static card
feed on GitHub Pages. No servers, no database, no pipeline stages.

## How it works

- `sources.txt` — feeds to pull, one per line (`Name | url`)
- `prompt.md` — the editorial brief; edit this to change taste
- `scripts/build_feed.py` — fetch, curate, merge into `docs/feed.json` (keeps 14 days)
- `.github/workflows/feed.yml` — runs daily at 02:30 UTC and on demand
- `docs/index.html` — the feed page

Failures are visible by design. The page header shows the last run time and
per-source status, GitHub emails you when a run fails, and a failed run never
touches the previous feed.

## Publish (one time)

From this folder, with the [GitHub CLI](https://cli.github.com) logged in:

```
gh repo create viks-news --public --source=. --push
gh secret set ANTHROPIC_API_KEY
```

Paste an API key from console.anthropic.com when prompted. Then:

1. Repo Settings → Pages → Deploy from a branch → `main`, folder `/docs`
2. Actions tab → "Build feed" → Run workflow
3. Read at `https://<your-username>.github.io/viks-news/`

Note: the repo and the page are public. GitHub Pages on a private repo needs a
paid plan, and the published page stays public either way outside Enterprise.

## Daily use

- Read the page. Friday's digest queue is simply the week's cards.
- A broken source is named in the page header. Fix or remove its line in
  `sources.txt`.
- Tune curation by editing `prompt.md`.
- Check sources without spending an API call:
  `python scripts/build_feed.py --dry-run`

## Knobs

Environment variables, settable in `feed.yml`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `MODEL` | `claude-sonnet-4-6` | Model for the curation call |
| `LOOKBACK_HOURS` | `36` | How far back to accept new items |
| `KEEP_DAYS` | `14` | Days of history kept on the page |
| `MAX_CANDIDATES` | `120` | Cap on items sent to the API per run |

## Custom domain

Repo Settings → Pages → Custom domain, plus a CNAME record at your DNS host.
