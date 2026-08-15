# r6-notifier

Twice-daily Discord notifications for Rainbow Six Siege esports:
- posts when a watched Twitch channel (default `rainbow6`) goes live
- posts upcoming matches (teams, tournament, time in Europe/Paris, Twitch link) from [Liquipedia](https://liquipedia.net/rainbowsix/Liquipedia:Matches)

No login, no automation of your Twitch account — just polling public data on a schedule.

## Setup

1. Create a Discord webhook: channel Settings -> Integrations -> Webhooks -> New Webhook -> Copy URL.
2. Register a free Twitch app at [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps) to get a Client ID + Secret (Client Credentials flow, no user login required).
3. Copy `.env.example` to `.env` and fill in the values.
4. `docker compose up -d --build`

State (which matches/live-transitions were already notified) is persisted in `./data/state.json`.
