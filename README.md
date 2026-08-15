# r6-notifier

Two small Docker services that track Rainbow Six Siege esports from public data — no login, no automation of your Twitch account, just polling public sources on a schedule.

**🎮 Live page: [revanito.github.io/r6-notifier](https://revanito.github.io/r6-notifier/)**

## What it does

**`r6-notifier`** — posts to a Discord webhook, twice a day (default 08:00 / 20:00 Europe/Paris):
- when a watched Twitch channel (default `rainbow6`) goes live
- upcoming matches in the next ~30 hours (teams, tournament, time, Twitch link)

**`r6-site`** — regenerates a static results/schedule page, every 10 minutes if anything changed, and pushes it to `docs/` on this repo's `main` branch. GitHub Pages serves that folder. Sections: Live now, Upcoming today, Upcoming (next 30 days), Recent results (last 7 days).

## Data sources

Two sources, merged:
- **Ubisoft's official esports page** — authoritative (has a real `live` flag and final scores), but only covers whatever event Ubisoft is currently spotlighting on that page.
- **[Liquipedia](https://liquipedia.net/rainbowsix/Liquipedia:Matches)** — much broader coverage (every tracked tournament), but occasionally lags on team reveals for not-yet-started bracket slots, and its "is this live" signal isn't trustworthy on its own.

Because of that, Ubisoft's feed wins on overlap and is the *only* source trusted for "live right now"; Liquipedia fills in everything Ubisoft's narrow feed doesn't cover. Matches are de-duplicated by timestamp, and `TBD vs TBD` placeholder slots from Liquipedia are dropped in favor of Ubisoft's resolved data when available.

## Setup

1. Create a Discord webhook: channel Settings → Integrations → Webhooks → New Webhook → copy the URL.
2. Register a free Twitch app at [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) to get a Client ID + Secret (Client Credentials flow — no user login, no special permissions, just app registration).
3. Generate a fine-grained GitHub Personal Access Token scoped to just this repo, with **Contents: Read and write** — Settings → Developer settings → Personal access tokens → Fine-grained tokens.
4. Enable GitHub Pages on this repo: Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, folder: `/docs` → Save. (The page won't render until `r6-site` pushes its first `docs/index.html` — see below.)
5. Copy `.env.example` to `.env` and fill in the values from steps 1–3.
6. `docker compose up -d --build`

That starts both services. State (which matches/live-transitions were already notified) is persisted in `./data/state.json`; the site generator's own git clone lives in `./site-repo/`.

## Notes

- `main.py` is the Discord notifier; `webgen.py` is the site generator — deliberately not named `site.py`, since that shadows Python's built-in `site` module.
- `sources.py` holds all the fetch/merge/classify logic shared by both.
- Set `RUN_ON_START=true` in `.env` to make either service run once immediately on startup instead of waiting for its first scheduled slot — useful when testing.
