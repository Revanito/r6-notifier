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

DROPS_URL = "https://twitchdrops.app/game/tom-clancys-rainbow-six-siege"
DROPS_CHANNEL_RE = re.compile(r"twitch\.tv/([^/?#\"]+)", re.I)

MATCH_DEDUP_BUCKET_SECONDS = 300  # matches within this window are treated as the same match across sources


def get_twitch_token(client_id, client_secret):
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_twitch_live_info(client_id, client_secret, channels):
    """Live status for a set of channel logins. Returns {login: {title, game, viewers}}."""
    channels = sorted({c.lower() for c in channels if c})
    if not client_id or not client_secret or not channels:
        return {}

    token = get_twitch_token(client_id, client_secret)
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    params = [("user_login", c) for c in channels]
    resp = requests.get("https://api.twitch.tv/helix/streams", headers=headers, params=params, timeout=15)
    resp.raise_for_status()

    info = {}
    for stream in resp.json().get("data", []):
        login = stream["user_login"].lower()
        title = stream.get("title", "")
        info[login] = {
            "title": title,
            "game": stream.get("game_name", ""),
            "viewers": stream.get("viewer_count", 0),
        }
    return info


def fetch_active_drops():
    """Scrapes twitchdrops.app's public R6 Siege page for real drops-campaign
    data: which channels are currently eligible for drops, and what the
    rewards actually are. This site publishes a plain-text chatbot API
    (twitchdrops.app/api/chatbot/...) built for public consumption, so
    scraping its HTML for the same data is in the same spirit.

    Returns (rewards, active_channels):
    - rewards: [{"name", "watch_time", "campaign"}, ...]
    - active_channels: lowercased set of channel logins eligible right now
      (campaign banners whose countdown hasn't ended yet)
    """
    resp = requests.get(DROPS_URL, headers={"User-Agent": UBISOFT_UA}, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rewards = []
    for card in soup.select(".drop-card"):
        name_el = card.select_one(".drop-name")
        time_el = card.select_one(".drop-time")
        campaign_el = card.select_one(".drop-campaign")
        rewards.append({
            "name": name_el.get_text(strip=True) if name_el else "",
            "watch_time": time_el.get_text(strip=True) if time_el else "",
            "campaign": campaign_el.get_text(strip=True) if campaign_el else "",
        })

    now_ms = time.time() * 1000
    active_channels = set()
    for banner in soup.select(".campaign-banner"):
        timer = banner.select_one(".cb-timer[data-end-ts]")
        if timer:
            try:
                if float(timer["data-end-ts"]) <= now_ms:
                    continue  # campaign already ended
            except (ValueError, TypeError):
                pass
        for link in banner.select("a.channel-link"):
            m = DROPS_CHANNEL_RE.search(link.get("href", ""))
            if m:
                active_channels.add(m.group(1).lower())

    return rewards, active_channels


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


def build_team_links(html):
    """Team name -> Liquipedia roster page URL, built from every team link
    on the page (broader than any single match, so a team seen once gets
    linked everywhere it appears - Ubisoft-sourced matches included, since
    they carry no such link of their own)."""
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    for a in soup.select(".match-info-header-opponent .name a"):
        raw_title = a.get("title", a.get_text(strip=True))
        if "(page does not exist)" in raw_title:
            continue  # red link - no real roster page to send people to
        name = re.sub(r"\s*\(page does not exist\)$", "", raw_title)
        href = a.get("href")
        if href and name not in links:
            links[name] = "https://liquipedia.net" + href
    return links


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
    """Primary source wins on overlap, but backfills fields it lacks (e.g.
    Ubisoft never exposes a twitch_channel) from the matching secondary
    entry instead of just discarding it. Secondary entries with no bucket
    match in primary are appended as-is."""
    primary_by_bucket = {_bucket(m["timestamp"]): m for m in primary}
    combined = list(primary)
    for match in secondary:
        if drop_tbd and "TBD" in match["teams"]:
            continue
        bucket = _bucket(match["timestamp"])
        existing = primary_by_bucket.get(bucket)
        if existing is not None:
            if not existing.get("twitch_channel") and match.get("twitch_channel"):
                existing["twitch_channel"] = match["twitch_channel"]
            continue
        combined.append(match)
    return combined


def gather_all_matches():
    """Fetch + merge both sources. Returns (all_matches, ubisoft_live_channels, team_links)."""
    ubi_matches, ubi_live_channels = [], []
    try:
        ubi_matches, ubi_live_channels = fetch_ubisoft_matches()
    except Exception:
        log.exception("ubisoft fetch failed")

    lp_matches = []
    team_links = {}
    try:
        html = fetch_liquipedia_html()
        lp_matches = parse_liquipedia_matches(html)
        team_links = build_team_links(html)
    except Exception:
        log.exception("liquipedia fetch failed")

    combined = _merge(ubi_matches, lp_matches)
    return combined, ubi_live_channels, team_links


def split_by_status(matches, now=None):
    """Returns (live, upcoming, completed) lists."""
    now = now if now is not None else time.time()
    live = [m for m in matches if m.get("live")]
    upcoming = [m for m in matches if not m.get("live") and not m.get("finished") and m["timestamp"] > now]
    completed = [m for m in matches if m.get("finished")]
    return live, upcoming, completed
