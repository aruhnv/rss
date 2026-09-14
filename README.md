# Simple RSS

A text-only feed reader that is just a static page. GitHub Actions fetches
every feed in `feeds.opml` every 30 minutes, writes `docs/index.html`, and
GitHub Pages serves it.

## Setup (once)

1. Create a new GitHub repo (public is simplest: unlimited Actions minutes) and push these files.
2. Repo → Settings → Pages → Source: "Deploy from a branch", branch `main`, folder `/docs`. Save.
3. Repo → Actions → "build feeds" → "Run workflow" to do the first build (the schedule takes over after that).
4. Open `https://<user>.github.io/<repo>/`.

## Day to day

- Add or remove feeds: edit `feeds.opml` (the `category` attribute is the comma-separated tag list). Pushing triggers a rebuild.
- Dead feeds: `docs/failures.txt` (linked at the bottom of the page) lists every feed that failed on the last run and how many runs in a row.
- Read/unread and the selected tag are stored in your browser's localStorage, not synced.
- Tuning: `KEEP_DAYS`, `MIN_PER_FEED`, `MAX_PER_FEED` at the top of `build.py`.

## Local run

    pip install -r requirements.txt
    python build.py
    open docs/index.html
