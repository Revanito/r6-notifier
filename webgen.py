import html
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import schedule

import sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("r6-site")

REPO_URL = os.environ["SITE_REPO_URL"]  # e.g. https://github.com/Revanito/r6-notifier.git
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GIT_USER_NAME = os.environ.get("GIT_USER_NAME", "r6-notifier-bot")
GIT_USER_EMAIL = os.environ.get("GIT_USER_EMAIL", "r6-notifier-bot@users.noreply.github.com")

CLONE_DIR = os.environ.get("SITE_CLONE_DIR", "/repo")
DOCS_SUBDIR = os.environ.get("SITE_DOCS_SUBDIR", "docs")
BRANCH = os.environ.get("SITE_BRANCH", "main")

BUILD_INTERVAL_MINUTES = int(os.environ.get("SITE_BUILD_INTERVAL_MINUTES", "10"))
TZ_NAME = os.environ.get("TZ_NAME", "Europe/Paris")
LOCAL_TZ = ZoneInfo(TZ_NAME)
RUN_ON_START = os.environ.get("RUN_ON_START", "false").lower() == "true"

UPCOMING_WINDOW_DAYS = float(os.environ.get("UPCOMING_WINDOW_DAYS", "30"))
RESULTS_WINDOW_DAYS = float(os.environ.get("RESULTS_WINDOW_DAYS", "7"))

# Optional: same Twitch app credentials as the Discord notifier. If set, the
# site can show non-match broadcasts (reveal streams, showcases, ...) as
# their own "live" card, and tag any live card with a drops badge when the
# stream title mentions it. If unset, the site just skips this - it still
# works fine on Ubisoft/Liquipedia match data alone.
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "")
TWITCH_CHANNELS = [c.strip().lower() for c in os.environ.get("TWITCH_CHANNELS", "rainbow6").split(",") if c.strip()]


def authed_repo_url():
    if REPO_URL.startswith("https://"):
        return REPO_URL.replace("https://", f"https://{GITHUB_TOKEN}@", 1)
    return REPO_URL


def _redact(text):
    return text.replace(GITHUB_TOKEN, "***") if GITHUB_TOKEN else text


def _run(args, cwd=CLONE_DIR):
    """Like subprocess.run(check=True), but never lets the token reach logs
    or exception messages — git argv (and CalledProcessError's repr of it)
    would otherwise leak it verbatim."""
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {_redact(' '.join(args))}\n{_redact(result.stderr)}"
        )
    return result


def run_git(args, cwd=CLONE_DIR):
    return _run(["git"] + args, cwd=cwd)


def ensure_repo():
    if not os.path.isdir(os.path.join(CLONE_DIR, ".git")):
        log.info("cloning repo into %s", CLONE_DIR)
        os.makedirs(CLONE_DIR, exist_ok=True)
        _run(["git", "clone", "--branch", BRANCH, authed_repo_url(), CLONE_DIR], cwd=None)
        run_git(["config", "user.name", GIT_USER_NAME])
        run_git(["config", "user.email", GIT_USER_EMAIL])
    else:
        run_git(["fetch", "origin", BRANCH])
        run_git(["checkout", BRANCH])
        run_git(["reset", "--hard", f"origin/{BRANCH}"])


def fmt_dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(LOCAL_TZ).strftime("%a %d %b, %H:%M")


def e(text):
    return html.escape(str(text))


def render_team(name, side_class, team_links):
    url = team_links.get(name)
    if url:
        return f'<a class="team {side_class}" href="{e(url)}" target="_blank" rel="noopener">{e(name)}</a>'
    return f'<span class="team {side_class}">{e(name)}</span>'


def render_match_row(match, kind, drops=False, team_links=None):
    team_links = team_links or {}
    teams = match["teams"]
    when = fmt_dt(match["timestamp"])
    score_html = ""
    if kind in ("live", "completed") and match.get("score"):
        s0, s1 = match["score"]
        w = match.get("winner_index")
        t0_cls = " winner" if w == 0 else ""
        t1_cls = " winner" if w == 1 else ""
        score_html = (
            f'<span class="score"><span class="digit{t0_cls}">{e(s0)}</span>'
            f'<span class="dash">–</span><span class="digit{t1_cls}">{e(s1)}</span></span>'
        )
    else:
        score_html = '<span class="vs">vs</span>'

    twitch_html = ""
    if match.get("twitch_channel"):
        twitch_html = f'<a class="twitch-link" href="https://www.twitch.tv/{e(match["twitch_channel"])}" target="_blank" rel="noopener">Watch on Twitch ↗</a>'

    badge = {
        "live": '<span class="badge badge-live"><span class="dot"></span>Live</span>',
        "upcoming": "",
        "completed": '<span class="badge badge-done">Final</span>',
    }[kind]
    drops_html = '<span class="badge badge-drops">Drops enabled</span>' if drops else ""

    return f"""
    <article class="ticket ticket-{kind}">
      <div class="ticket-row">
        {render_team(teams[0], "team-a", team_links)}
        {score_html}
        {render_team(teams[1], "team-b", team_links)}
      </div>
      <div class="ticket-meta">
        <span class="tournament">{e(match["tournament"])}</span>
        <span class="dot-sep">·</span>
        <span class="when">{e(when)} Paris</span>
        {twitch_html}
        {badge}
        {drops_html}
      </div>
    </article>"""


def render_broadcast_row(b):
    """A live Twitch stream not tied to a tracked match - e.g. a reveal
    show, dev stream, or anything else airing on a watched channel."""
    game_html = f' · {e(b["game"])}' if b.get("game") else ""
    drops_html = '<span class="badge badge-drops">Drops enabled</span>' if b.get("has_drops") else ""

    return f"""
    <article class="ticket ticket-live ticket-broadcast">
      <div class="ticket-row ticket-row-broadcast">
        <span class="broadcast-title">{e(b["title"] or b["channel"])}</span>
      </div>
      <div class="ticket-meta">
        <span class="tournament">twitch.tv/{e(b["channel"])}{game_html}</span>
        <a class="twitch-link" href="https://www.twitch.tv/{e(b["channel"])}" target="_blank" rel="noopener">Watch on Twitch ↗</a>
        <span class="badge badge-live"><span class="dot"></span>Live</span>
        {drops_html}
      </div>
    </article>"""


def render_reward_card(r):
    img_html = f'<img class="reward-img" src="{e(r["image"])}" alt="{e(r["name"])}" loading="lazy">' if r.get("image") else ""
    return f"""
    <article class="reward-card">
      {img_html}
      <div class="reward-name">{e(r["name"])}</div>
      <div class="reward-time">{e(r["watch_time"])}</div>
      <div class="reward-campaign">{e(r["campaign"])}</div>
    </article>"""


def render_rewards_section(rewards):
    if not rewards:
        return ""
    cards = "".join(render_reward_card(r) for r in rewards)
    return f"""
    <section class="section-rewards">
      <h2>Active drops</h2>
      <div class="rewards-grid">{cards}</div>
    </section>"""


def render_section(title, row_htmls, kind, empty_text):
    if not row_htmls:
        body = f'<p class="empty">{e(empty_text)}</p>'
    else:
        body = "".join(row_htmls)
    return f"""
    <section class="section-{kind}">
      <h2>{e(title)}</h2>
      <div class="ticket-list">{body}</div>
    </section>"""


def build_html(live, upcoming, completed, generated_at, twitch_info=None, broadcasts=None, drops_channels=None, team_links=None, rewards=None):
    twitch_info = twitch_info or {}
    broadcasts = broadcasts or []
    drops_channels = drops_channels or set()
    team_links = team_links or {}
    rewards = rewards or []

    live_sorted = sorted(live, key=lambda m: m["timestamp"])
    upcoming_sorted = sorted(upcoming, key=lambda m: m["timestamp"])
    completed_sorted = sorted(completed, key=lambda m: m["timestamp"], reverse=True)

    sections = ""
    today = datetime.now(tz=LOCAL_TZ).date()
    today_matches = [m for m in upcoming_sorted if datetime.fromtimestamp(m["timestamp"], tz=timezone.utc).astimezone(LOCAL_TZ).date() == today]
    later_matches = [m for m in upcoming_sorted if m not in today_matches]

    def has_drops(match):
        channel = (match.get("twitch_channel") or "").lower()
        return channel in drops_channels

    default_channel = TWITCH_CHANNELS[0] if TWITCH_CHANNELS else None

    def with_fallback_channel(match):
        # Liquipedia is our only source for a match's specific channel, but
        # it drops a match's countdown timer entirely once it goes truly
        # live (different DOM), so a live match can lose its only channel
        # source right when it matters most. Fall back to the main watched
        # channel rather than show nothing.
        if match.get("twitch_channel"):
            return match
        return {**match, "twitch_channel": default_channel}

    live_sorted = [with_fallback_channel(m) for m in live_sorted]
    today_matches = [with_fallback_channel(m) for m in today_matches]
    later_matches = [with_fallback_channel(m) for m in later_matches]

    live_rows = [render_match_row(m, "live", drops=has_drops(m), team_links=team_links) for m in live_sorted]
    live_rows += [render_broadcast_row(b) for b in broadcasts]
    today_rows = [render_match_row(m, "upcoming", drops=has_drops(m), team_links=team_links) for m in today_matches]
    later_rows = [render_match_row(m, "upcoming", drops=has_drops(m), team_links=team_links) for m in later_matches]
    completed_rows = [render_match_row(m, "completed", team_links=team_links) for m in completed_sorted]

    if live_rows:
        sections += render_section("Live now", live_rows, "live", "")
    sections += render_rewards_section(rewards)
    if today_rows:
        sections += render_section("Upcoming today", today_rows, "upcoming", "")
    sections += render_section(f"Upcoming (next {int(UPCOMING_WINDOW_DAYS)} days)", later_rows, "upcoming", "No further matches scheduled in this window.")
    sections += render_section(f"Recent results (last {int(RESULTS_WINDOW_DAYS)} days)", completed_rows, "completed", "No recent results.")

    generated_str = generated_at.astimezone(LOCAL_TZ).strftime("%a %d %b %Y, %H:%M (Paris time)")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>R6 Ops Board</title>
<link rel="icon" type="image/png" href="favicon.png">
<style>
:root {{
  --bg: #eef0f2;
  --bg-wash: #e4e7ea;
  --card: #ffffff;
  --text: #12151a;
  --text-dim: #5b6470;
  --border: #d8dce1;
  --accent: #d9600a;
  --accent-ink: #ffffff;
  --live: #d0271f;
  --win: #12151a;
  --lose: #a7adb6;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #0d0f12; --bg-wash: #15181c; --card: #15181c; --text: #eceef0;
    --text-dim: #838d97; --border: #262b31; --accent: #ff8c3a; --accent-ink: #16110a;
    --live: #ff453a; --win: #ffffff; --lose: #5c636b;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #0d0f12; --bg-wash: #15181c; --card: #15181c; --text: #eceef0;
  --text-dim: #838d97; --border: #262b31; --accent: #ff8c3a; --accent-ink: #16110a;
  --live: #ff453a; --win: #ffffff; --lose: #5c636b;
}}
* {{ box-sizing: border-box; }}
html {{ background: var(--bg); }}
body {{
  margin: 0; padding: 0 1rem 4rem; background: var(--bg); color: var(--text);
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}}
main {{ max-width: 980px; margin: 0 auto; padding: 0 0.5rem; }}
header {{
  padding: 2.5rem 0 1.75rem;
  border-bottom: 1px solid var(--border);
  margin-bottom: 2rem;
}}
.eyebrow {{
  font: 700 0.72rem/1 -apple-system, "Segoe UI", Roboto, sans-serif;
  text-transform: uppercase; letter-spacing: 0.14em; color: var(--accent); margin: 0 0 0.6rem;
}}
h1 {{
  font: 800 1.9rem/1.1 "Segoe UI", -apple-system, Roboto, "Arial Narrow", sans-serif;
  font-stretch: condensed; text-transform: uppercase; letter-spacing: 0.01em;
  margin: 0 0 0.4rem; text-wrap: balance;
}}
.subtitle {{ color: var(--text-dim); font-size: 0.92rem; margin: 0; }}
h2 {{
  font: 700 0.78rem/1 -apple-system, "Segoe UI", sans-serif;
  text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-dim);
  margin: 0 0 0.85rem; display: flex; align-items: center; gap: 0.5rem;
}}
section {{ margin-bottom: 2.25rem; }}
.section-live h2 {{ color: var(--live); }}
.ticket-list {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 0.6rem; align-items: start; }}
.section-completed .ticket-list {{ grid-template-columns: 1fr; }}
.ticket {{
  background: var(--card); border: 1px solid var(--border); border-left: 3px solid var(--border);
  border-radius: 4px; padding: 0.85rem 1rem; position: relative;
}}
.ticket-live {{ border-left-color: var(--live); padding-top: 2.1rem; }}
.ticket-completed {{ opacity: 0.88; }}
.ticket-row {{ display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 0.6rem; }}
.team {{
  font: 700 1.15rem/1.25 "Segoe UI", -apple-system, "Arial Narrow", sans-serif;
  font-stretch: condensed; letter-spacing: 0.01em;
}}
a.team {{ color: inherit; text-decoration: none; }}
a.team:hover {{ color: var(--accent); text-decoration: underline; }}
.team-a {{ text-align: right; }}
.team-b {{ text-align: left; }}
.vs {{ color: var(--text-dim); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; }}
.score {{
  display: flex; align-items: baseline; gap: 0.3rem; justify-content: center;
  font: 700 1.2rem/1 ui-monospace, "Cascadia Mono", Consolas, "SFMono-Regular", monospace;
  font-variant-numeric: tabular-nums;
}}
.score .dash {{ color: var(--text-dim); font-weight: 400; }}
.score .digit {{ color: var(--lose); min-width: 1ch; text-align: center; }}
.score .digit.winner {{ color: var(--win); }}
.ticket-meta {{
  display: flex; align-items: center; flex-wrap: wrap; gap: 0.5rem;
  margin-top: 0.55rem; font-size: 0.78rem; color: var(--text-dim);
}}
.dot-sep {{ opacity: 0.6; }}
.when {{ font-variant-numeric: tabular-nums; }}
.twitch-link {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
.twitch-link:hover {{ text-decoration: underline; }}
.badge {{
  margin-left: auto; display: inline-flex; align-items: center; gap: 0.35rem;
  font: 700 0.68rem/1 -apple-system, sans-serif; text-transform: uppercase; letter-spacing: 0.06em;
  padding: 0.25rem 0.55rem; border-radius: 999px;
}}
.badge-live {{
  background: var(--live); color: #fff;
  position: absolute; top: 0.7rem; right: 0.85rem; margin-left: 0;
}}
.badge-live .dot {{
  width: 6px; height: 6px; border-radius: 50%; background: #fff;
  animation: pulse 1.6s ease-in-out infinite;
}}
@media (prefers-reduced-motion: reduce) {{ .badge-live .dot {{ animation: none; }} }}
@keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.35; }} }}
.badge-done {{ background: var(--bg-wash); color: var(--text-dim); border: 1px solid var(--border); }}
.badge-drops {{ background: var(--accent); color: var(--accent-ink); }}
.ticket-row-broadcast {{ display: block; }}
.broadcast-title {{
  font: 700 0.98rem/1.3 "Segoe UI", -apple-system, "Arial Narrow", sans-serif;
  font-stretch: condensed; letter-spacing: 0.01em;
}}
.section-rewards h2 {{ color: var(--accent); }}
.rewards-grid {{ display: flex; flex-wrap: wrap; justify-content: center; gap: 0.6rem; }}
.reward-card {{
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 0.75rem; display: flex; flex-direction: column; align-items: center;
  text-align: center; gap: 0.35rem; width: 150px;
}}
.reward-img {{
  width: 64px; height: 64px; object-fit: contain; border-radius: 6px;
  background: var(--bg-wash); padding: 0.3rem;
}}
.reward-name {{ font: 700 0.82rem/1.25 "Segoe UI", -apple-system, sans-serif; }}
.reward-time {{
  font: 700 0.7rem/1 -apple-system, sans-serif; color: var(--accent);
  text-transform: uppercase; letter-spacing: 0.03em;
}}
.reward-campaign {{ font-size: 0.7rem; color: var(--text-dim); }}
.empty {{
  color: var(--text-dim); font-size: 0.88rem; padding: 1rem; border: 1px dashed var(--border);
  border-radius: 4px; background: var(--bg-wash);
}}
footer {{
  margin-top: 3rem; padding-top: 1.5rem; border-top: 1px solid var(--border);
  color: var(--text-dim); font-size: 0.76rem; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;
}}
a {{ color: inherit; }}
</style>
</head>
<body>
<main>
  <header>
    <p class="eyebrow">Rainbow Six Siege · Esports</p>
    <h1>Ops Board</h1>
    <p class="subtitle">Live status, upcoming matches and recent results, pulled from Ubisoft's official feed and Liquipedia.</p>
  </header>
  {sections}
  <footer>
    <span>Updated {e(generated_str)}</span>
    <span>Sources: Ubisoft, Liquipedia, twitchdrops.app</span>
  </footer>
</main>
</body>
</html>
"""


def build_and_commit():
    log.info("building site")
    ensure_repo()

    matches, ubi_live_channels, team_links = sources.gather_all_matches()
    now = time.time()
    live, upcoming, completed = sources.split_by_status(matches, now=now)

    upcoming = [m for m in upcoming if m["timestamp"] <= now + UPCOMING_WINDOW_DAYS * 86400]
    completed = [m for m in completed if m["timestamp"] >= now - RESULTS_WINDOW_DAYS * 86400]

    rewards, drops_channels = [], set()
    try:
        rewards, drops_channels = sources.fetch_active_drops()
    except Exception:
        log.exception("twitch drops fetch failed")

    twitch_info, broadcasts = {}, []
    if TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET:
        channels_to_check = set(TWITCH_CHANNELS) | set(ubi_live_channels)
        channels_to_check.update(m["twitch_channel"].lower() for m in matches if m.get("twitch_channel"))
        try:
            twitch_info = sources.fetch_twitch_live_info(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, channels_to_check)
        except Exception:
            log.exception("twitch live info fetch failed")

        matched_channels = {m["twitch_channel"].lower() for m in live if m.get("twitch_channel")}
        broadcasts = [
            {"channel": channel, "has_drops": channel in drops_channels, **info}
            for channel, info in twitch_info.items()
            if channel not in matched_channels
        ]

    generated_at = datetime.now(tz=timezone.utc)
    page = build_html(
        live, upcoming, completed, generated_at,
        twitch_info=twitch_info, broadcasts=broadcasts, drops_channels=drops_channels,
        team_links=team_links, rewards=rewards,
    )

    docs_dir = os.path.join(CLONE_DIR, DOCS_SUBDIR)
    os.makedirs(docs_dir, exist_ok=True)
    with open(os.path.join(docs_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    nojekyll = os.path.join(docs_dir, ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w").close()

    run_git(["add", DOCS_SUBDIR])
    status = run_git(["status", "--porcelain", "--", DOCS_SUBDIR])
    if not status.stdout.strip():
        log.info("no changes, skipping commit")
        return

    run_git(["commit", "-m", f"Update site {generated_at.isoformat()}"])
    run_git(["push", "origin", BRANCH])
    log.info("pushed site update")


def safe_build_and_commit():
    try:
        build_and_commit()
    except Exception:
        log.exception("site build failed, will retry next interval")


def main():
    log.info("r6-site starting, build interval=%d min", BUILD_INTERVAL_MINUTES)

    schedule.every(BUILD_INTERVAL_MINUTES).minutes.do(safe_build_and_commit)

    if RUN_ON_START:
        safe_build_and_commit()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
