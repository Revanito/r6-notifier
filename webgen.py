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


def render_match_row(match, kind):
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

    return f"""
    <article class="ticket ticket-{kind}">
      <div class="ticket-row">
        <span class="team team-a">{e(teams[0])}</span>
        {score_html}
        <span class="team team-b">{e(teams[1])}</span>
      </div>
      <div class="ticket-meta">
        <span class="tournament">{e(match["tournament"])}</span>
        <span class="dot-sep">·</span>
        <span class="when">{e(when)} Paris</span>
        {twitch_html}
        {badge}
      </div>
    </article>"""


def render_section(title, matches, kind, empty_text):
    if not matches:
        body = f'<p class="empty">{e(empty_text)}</p>'
    else:
        body = "".join(render_match_row(m, kind) for m in matches)
    return f"""
    <section class="section-{kind}">
      <h2>{e(title)}</h2>
      <div class="ticket-list">{body}</div>
    </section>"""


def build_html(live, upcoming, completed, generated_at):
    live_sorted = sorted(live, key=lambda m: m["timestamp"])
    upcoming_sorted = sorted(upcoming, key=lambda m: m["timestamp"])
    completed_sorted = sorted(completed, key=lambda m: m["timestamp"], reverse=True)

    sections = ""
    today = datetime.now(tz=LOCAL_TZ).date()
    today_matches = [m for m in upcoming_sorted if datetime.fromtimestamp(m["timestamp"], tz=timezone.utc).astimezone(LOCAL_TZ).date() == today]
    later_matches = [m for m in upcoming_sorted if m not in today_matches]

    if live_sorted:
        sections += render_section("Live now", live_sorted, "live", "")
    if today_matches:
        sections += render_section("Upcoming today", today_matches, "upcoming", "")
    sections += render_section(f"Upcoming (next {int(UPCOMING_WINDOW_DAYS)} days)", later_matches, "upcoming", "No further matches scheduled in this window.")
    sections += render_section(f"Recent results (last {int(RESULTS_WINDOW_DAYS)} days)", completed_sorted, "completed", "No recent results.")

    generated_str = generated_at.astimezone(LOCAL_TZ).strftime("%a %d %b %Y, %H:%M (Paris time)")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>R6 Ops Board</title>
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
  border-radius: 4px; padding: 0.85rem 1rem;
}}
.ticket-live {{ border-left-color: var(--live); }}
.ticket-completed {{ opacity: 0.88; }}
.ticket-row {{ display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; gap: 0.6rem; }}
.team {{
  font: 700 0.98rem/1.25 "Segoe UI", -apple-system, "Arial Narrow", sans-serif;
  font-stretch: condensed; letter-spacing: 0.01em;
}}
.team-a {{ text-align: right; }}
.team-b {{ text-align: left; }}
.vs {{ color: var(--text-dim); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; }}
.score {{
  display: flex; align-items: baseline; gap: 0.3rem; justify-content: center;
  font: 700 1.05rem/1 ui-monospace, "Cascadia Mono", Consolas, "SFMono-Regular", monospace;
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
.badge-live {{ background: var(--live); color: #fff; }}
.badge-live .dot {{
  width: 6px; height: 6px; border-radius: 50%; background: #fff;
  animation: pulse 1.6s ease-in-out infinite;
}}
@media (prefers-reduced-motion: reduce) {{ .badge-live .dot {{ animation: none; }} }}
@keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.35; }} }}
.badge-done {{ background: var(--bg-wash); color: var(--text-dim); border: 1px solid var(--border); }}
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
    <span>Sources: Ubisoft, Liquipedia</span>
  </footer>
</main>
</body>
</html>
"""


def build_and_commit():
    log.info("building site")
    ensure_repo()

    matches, _ = sources.gather_all_matches()
    now = time.time()
    live, upcoming, completed = sources.split_by_status(matches, now=now)

    upcoming = [m for m in upcoming if m["timestamp"] <= now + UPCOMING_WINDOW_DAYS * 86400]
    completed = [m for m in completed if m["timestamp"] >= now - RESULTS_WINDOW_DAYS * 86400]

    generated_at = datetime.now(tz=timezone.utc)
    page = build_html(live, upcoming, completed, generated_at)

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
