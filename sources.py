"""Shared fetch/parse logic for both the Discord notifier and the website generator.

Two public data sources, merged and de-duplicated:
- Ubisoft's official esports page (accurate, but only covers whatever event
  Ubisoft is currently spotlighting on that page)
- Liquipedia's site-wide match ticker (broader coverage, occasionally lags
  on team reveals for not-yet-started bracket slots)
"""
import json
import logging
import re
import time
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("r6-notifier.sources")

LIQUIPEDIA_API = "https://liquipedia.net/rainbowsix/api.php"
LIQUIPEDIA_UA = "r6-notifier/1.0 (personal notification bot; contact via github.com/Revanito/r6-notifier)"

UBISOFT_URL = "https://www.ubisoft.com/en-us/esports/rainbow-six/siege"
UBISOFT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)

MATCH_DEDUP_BUCKET_SECONDS = 300  # matches within this window are treated as the same match across sources


def _make_key(ts, teams):
    return f"{ts}|{teams[0]}|{teams[1]}"


def fetch_ubisoft_matches():
    """Official schedule/results for the currently-featured event. More
    authoritative than Liquipedia but limited in breadth."""
    resp = requests.get(UBISOFT_URL, headers={"User-Agent": UBISOFT_UA}, timeout=20)
    resp.raise_for_status()
    m = NEXT_DATA_RE.search(resp.text)
    if not m:
        return [], []
    data = json.loads(m.group(1))
    page_data = data["props"]["pageProps"]["pageData"]

    matches = []
    for match in page_data.get("matches", []):
        if match.get("isTimeTBD"):
            continue
        ts = match["timestamp"]
        teams = [match["team1"]["name"], match["team2"]["name"]]
        is_live = bool(match.get("live"))
        scores = match.get("scores") or {}
        score = (scores.get("team1", 0), scores.get("team2", 0))
        # NB: Ubisoft pre-populates "gameScores" with zeroed placeholder
        # entries for every possible map before a match even starts (e.g. 5
        # empty slots for a Bo5), so those can't be used as a "has a score"
        # signal - only the top-level series score and a past start time can.
        has_score = score != (0, 0)
        finished = (not is_live) and has_score and ts <= time.time()

        matches.append({
            "timestamp": ts,
            "teams": teams,
            "tournament": match.get("competition", {}).get("name", "Unknown tournament"),
            "twitch_channel": None,
            "live": is_live,
            "finished": finished,
            "score": score if (finished or is_live) else None,
            "key": _make_key(ts, teams),
        })

    live_channels = [c.lower() for c in page_data.get("liveChannels", [])]
    return matches, live_channels


def fetch_liquipedia_html():
    resp = requests.get(
        LIQUIPEDIA_API,
        headers={"User-Agent": LIQUIPEDIA_UA, "Accept-Encoding": "gzip"},
        params={"action": "parse", "page": "Liquipedia:Matches", "format": "json", "prop": "text"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["parse"]["text"]["*"]


def _extract_teams(match_div):
    return [
        re.sub(r"\s*\(page does not exist\)$", "", a.get("title", a.get_text(strip=True)))
        for a in match_div.select(".match-info-header-opponent .name a")
    ]


def _extract_tournament(match_div):
    el = match_div.select_one(".match-info-tournament-name a")
    return el.get_text(strip=True) if el else "Unknown tournament"


def _extract_twitch_channel(match_div):
    link = match_div.select_one('a[href*="Special:Stream/twitch/"]')
    if not link:
        return None
    m = re.search(r"Special:Stream/twitch/([^\"?#]+)", link["href"])
    return unquote(m.group(1)) if m else None


def parse_liquipedia_matches(html):
    """Returns all matches (upcoming, live, and recently completed) found
    anywhere on the page, tagged with their live/finished status."""
    soup = BeautifulSoup(html, "html.parser")
    matches = []

    for match_div in soup.select("div.match-info"):
        timer = match_div.select_one(".match-info-countdown .timer-object")
        if not timer or not timer.get("data-timestamp"):
            continue

        ts = int(timer["data-timestamp"])
        finished = timer.get("data-finished") == "finished"
        is_past = ts <= time.time()
        teams = _extract_teams(match_div)
        if len(teams) < 2:
            teams = teams + ["TBD"] * (2 - len(teams))

        score = None
        winner_idx = None
        if finished or is_past:
            score_spans = match_div.select(".match-info-header-scoreholder-score")
            if len(score_spans) >= 2:
                score = (score_spans[0].get_text(strip=True), score_spans[1].get_text(strip=True))
            opponents = match_div.select(".match-info-header-opponent")
            for i, opp in enumerate(opponents[:2]):
                if "match-info-header-winner" in opp.get("class", []):
                    winner_idx = i

        if not finished and is_past:
            # Past but never flagged "finished": Liquipedia's live/ticker state
            # can't be trusted to mean "live right now" (it also shows stale
            # entries from matches that already ended). If a score got
            # posted, treat it as a result; otherwise it's too ambiguous to
            # show at all rather than risk a false "LIVE" claim.
            if score and any(s.strip() for s in score):
                finished = True
            else:
                continue

        matches.append({
            "timestamp": ts,
            "teams": teams,
            "tournament": _extract_tournament(match_div),
            "twitch_channel": _extract_twitch_channel(match_div),
            "live": False,  # only Ubisoft's feed is trusted for "live right now"
            "finished": finished,
            "score": score,
            "winner_index": winner_idx,
            "key": _make_key(ts, teams),
        })

    return matches


def _bucket(ts):
    return round(ts / MATCH_DEDUP_BUCKET_SECONDS)


def _merge(primary, secondary, drop_tbd=True):
    """Primary source wins on overlap; secondary fills in gaps."""
    primary_buckets = {_bucket(m["timestamp"]) for m in primary}
    combined = list(primary)
    for match in secondary:
        if drop_tbd and "TBD" in match["teams"]:
            continue
        if _bucket(match["timestamp"]) in primary_buckets:
            continue
        combined.append(match)
    return combined


def gather_all_matches():
    """Fetch + merge both sources. Returns (all_matches, ubisoft_live_channels)."""
    ubi_matches, ubi_live_channels = [], []
    try:
        ubi_matches, ubi_live_channels = fetch_ubisoft_matches()
    except Exception:
        log.exception("ubisoft fetch failed")

    lp_matches = []
    try:
        html = fetch_liquipedia_html()
        lp_matches = parse_liquipedia_matches(html)
    except Exception:
        log.exception("liquipedia fetch failed")

    combined = _merge(ubi_matches, lp_matches)
    return combined, ubi_live_channels


def split_by_status(matches, now=None):
    """Returns (live, upcoming, completed) lists."""
    now = now if now is not None else time.time()
    live = [m for m in matches if m.get("live")]
    upcoming = [m for m in matches if not m.get("live") and not m.get("finished") and m["timestamp"] > now]
    completed = [m for m in matches if m.get("finished")]
    return live, upcoming, completed
