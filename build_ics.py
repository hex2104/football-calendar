#!/usr/bin/env python3
"""
Build an auto-updating .ics calendar feed of football fixtures and live scores.

Data source: football-data.org v4 (free tier covers PL, La Liga, Bundesliga,
Champions League and more). Set FOOTBALL_DATA_TOKEN in the environment.

Usage:
    python build_ics.py                 # uses config.yaml, writes to ./public
    python build_ics.py --config x.yaml --out ./dist
    python build_ics.py --offline fixture.json   # build from a saved payload (testing)
    python build_ics.py --previous ./old-public  # compare against a local build

By default each online build fetches the currently published feed from
base_url, so unchanged events keep their SEQUENCE and DTSTAMP and changed
ones always move forward.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

API_BASE = "https://api.football-data.org/v4"
PRODID = "-//football-calendar//EN"
USER_AGENT = "football-calendar/1.0"

# Fingerprint of an event's content, so the next build can tell whether it
# changed without comparing every property.
HASH_PROP = "X-FOOTBALL-CALENDAR-HASH"

# If a competition returns fewer than this share of the matches it returned
# in the previous build of the same season, treat the response as partial and
# refuse to publish. Set ALLOW_SHRINK=1 to override.
SHRINK_LIMIT = 0.5

# football-data.org competition codes available on the free tier
COMPETITION_NAMES = {
    "PL": "Premier League",
    "PD": "La Liga",
    "BL1": "Bundesliga",
    "CL": "Champions League",
    "SA": "Serie A",
    "FL1": "Ligue 1",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "ELC": "Championship",
    "BSA": "Brasileirao",
}

# Short tags used in event titles
COMPETITION_TAGS = {
    "PL": "PL",
    "PD": "LaLiga",
    "BL1": "BL",
    "CL": "UCL",
    "SA": "SerieA",
    "FL1": "L1",
    "DED": "ERE",
    "PPL": "PPL",
    "ELC": "ELC",
    "BSA": "BSA",
}

# Ordering matters: later statuses must rank higher so SEQUENCE only grows.
# POSTPONED sits at 0 because a postponed match goes back to TIMED once it is
# rescheduled; CANCELLED is terminal so it can safely sit at the top.
STATUS_RANK = {
    "SCHEDULED": 0,
    "TIMED": 0,
    "POSTPONED": 0,
    "SUSPENDED": 3,
    "IN_PLAY": 3,
    "PAUSED": 3,  # same rank as IN_PLAY: half time must not outrank the 2nd half
    "FINISHED": 5,
    "AWARDED": 5,
    "CANCELLED": 6,
}

# SCHEDULED means the date is known but the kickoff time is a placeholder
# (e.g. awaiting TV picks); a POSTPONED match's slot is no longer happening.
TENTATIVE_STATUSES = ("SCHEDULED", "POSTPONED")

# The free tier leaves venue out of match data, so venues come from each
# club's home ground (the teams endpoint). Stadiums rarely change, so the
# lookup is published as venues.json and only refreshed this often.
VENUES_MAX_AGE = dt.timedelta(days=7)

# Stages played at a neutral ground, where the home club's stadium is wrong.
NEUTRAL_STAGES = ("FINAL",)

# SEQUENCE carries a day counter so it still advances if a match is rescheduled
# without its status or scoreline changing.
SEQUENCE_EPOCH = dt.date(2020, 1, 1)


class ApiError(RuntimeError):
    """football-data.org could not be reached or refused the request."""


def load_config(path: str) -> dict:
    try:
        import yaml
    except ImportError:  # pragma: no cover
        sys.exit("PyYAML is required:  pip install pyyaml")
    return yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def retry_wait(err: urllib.error.HTTPError, attempt: int) -> int:
    """Seconds to wait before retrying a failed request."""
    if err.code == 429:
        # Free tier is 10 requests/minute; the API says when the window resets.
        reset = (err.headers.get("X-RequestCounter-Reset") or "") if err.headers else ""
        if reset.strip().isdigit():
            return min(int(reset) + 1, 65)
        return 20 * (attempt + 1)
    return 5 * (attempt + 1)


def api_get(path: str, token: str, retries: int = 4) -> dict:
    """
    GET a football-data.org endpoint, honouring the free tier's rate limit.

    Raises ApiError instead of returning nothing, so a failed fetch can never
    be mistaken for a competition that has no matches.
    """
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "X-Auth-Token": token,
        "User-Agent": USER_AGENT,
    })
    for attempt in range(retries):
        last = attempt == retries - 1
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code in (403, 404):
                raise ApiError(f"{url} -> HTTP {err.code} (not on your plan?)") from err
            if err.code != 429 and err.code < 500:
                raise ApiError(f"{url} -> HTTP {err.code}") from err
            if last:
                raise ApiError(f"{url} -> HTTP {err.code} after {retries} attempts") from err
            wait = retry_wait(err, attempt)
            print(f"  HTTP {err.code}, retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
        except (OSError, ValueError) as err:  # network errors, timeouts, bad JSON
            if last:
                raise ApiError(f"{url} -> {err}") from err
            wait = 5 * (attempt + 1)
            print(f"  network error ({err}), retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise ApiError(f"{url} -> no attempts made")


def read_previous(location: str, name: str) -> str | None:
    """A file from the last published build (URL or directory), or None."""
    if not location:
        return None
    if re.match(r"https?://", location):
        # Cache-buster: Pages sits behind a CDN that caches for ~10 minutes.
        url = f"{location.rstrip('/')}/{name}?nocache={int(time.time())}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8")
        except (OSError, ValueError) as err:
            print(f"  previous {name} unavailable ({err})", file=sys.stderr)
            return None
    path = pathlib.Path(location) / name
    return path.read_bytes().decode("utf-8") if path.exists() else None


# --------------------------------------------------------------------------
# ICS primitives
# --------------------------------------------------------------------------

def esc(text: str) -> str:
    """Escape a value for an ICS property (RFC 5545 §3.3.11)."""
    return (str(text)
            .replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\r\n", "\\n")
            .replace("\n", "\\n"))


def fold(line: str) -> str:
    """Fold a content line to 75 octets, splitting on byte boundaries."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, start = [], 0
    limit = 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        # don't split a multi-byte character
        while end > start and end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(raw[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines carry a leading space
    return "\r\n ".join(chunks)


def unfold(raw: str) -> list[str]:
    """Split an .ics document into logical (unfolded) content lines."""
    lines, buffer = [], ""
    for line in re.split(r"\r\n|\n", raw):
        if line.startswith(" "):
            buffer += line[1:]
        else:
            if buffer:
                lines.append(buffer)
            buffer = line
    if buffer:
        lines.append(buffer)
    return lines


def parse_events(raw: str) -> dict[str, dict[str, str]]:
    """Top-level properties of each VEVENT, keyed by UID."""
    events: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in unfold(raw):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current and "UID" in current:
                events[current["UID"]] = current
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            # setdefault: a VALARM's DESCRIPTION must not shadow the event's
            current.setdefault(key.split(";")[0], value)
    return events


def utc(stamp: dt.datetime) -> str:
    return stamp.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------
# Match -> event
# --------------------------------------------------------------------------

def team_labels(team: dict) -> list[str]:
    """All the names a team may be referred to by, lowercased."""
    out = []
    for key in ("name", "shortName", "tla"):
        value = team.get(key)
        if value:
            out.append(str(value).lower())
    return out


def team_matches(team: dict, want: str) -> bool:
    return any(want in label for label in team_labels(team))


def matches_watchlist(match: dict, watchlist: set[str]) -> bool:
    if not watchlist:
        return True  # empty list means "every match in the competition"
    return any(team_matches(match.get(side) or {}, want)
               for side in ("homeTeam", "awayTeam") for want in watchlist)


def watchlist_report(by_competition: dict[str, list[dict]], watchlist: set[str],
                     only_in: dict[str, set[str]]) -> list[str]:
    """Warnings for configured team names that match no club, or several."""
    targets = [(None, want) for want in sorted(watchlist)]
    targets += [(code, want) for code, names in sorted(only_in.items())
                for want in sorted(names)]
    warnings = []
    for scope, want in targets:
        clubs: dict[object, str] = {}
        for code, matches in by_competition.items():
            if scope and code != scope:
                continue
            for match in matches:
                for side in ("homeTeam", "awayTeam"):
                    team = match.get(side) or {}
                    if team_matches(team, want):
                        clubs[team.get("id") or team.get("name")] = display_name(team)
        where = f" (in {scope})" if scope else ""
        if not clubs:
            warnings.append(f"'{want}'{where} matches no club - "
                            "check the spelling with list_teams.py")
        elif len(clubs) > 1:
            warnings.append(f"'{want}'{where} matches {len(clubs)} clubs: "
                            f"{', '.join(sorted(clubs.values()))}")
    return warnings


def team_badge(team: dict, cfg: dict) -> str:
    """The configured colour dot for a team, matched like the watchlist."""
    for want, badge in (cfg.get("team_badges") or {}).items():
        want, badge = str(want).strip().lower(), str(badge or "").strip()
        if want and badge and team_matches(team, want):
            return badge
    return ""


def display_name(team: dict) -> str:
    return (team.get("shortName") or team.get("name")
            or team.get("tla") or "TBD")


def score_pair(match: dict, period: str = "fullTime") -> tuple[int | None, int | None]:
    block = ((match.get("score") or {}).get(period) or {})
    return block.get("home"), block.get("away")


def live_label(match: dict) -> str:
    """Short status tag for the title, e.g. LIVE 34', HT, FT."""
    status = match.get("status")
    minute = match.get("minute")
    if status == "IN_PLAY":
        return f"LIVE {minute}'" if minute else "LIVE"
    if status == "PAUSED":
        return "HT"
    if status in ("FINISHED", "AWARDED"):
        duration = (match.get("score") or {}).get("duration")
        if duration == "PENALTY_SHOOTOUT":
            return "FT"  # the shootout result already sits in the scoreline
        if duration == "EXTRA_TIME":
            return "AET"
        return "FT"
    if status == "SCHEDULED":
        return "TBC"  # kickoff time not confirmed yet
    if status == "POSTPONED":
        return "POSTPONED"
    if status == "SUSPENDED":
        return "SUSPENDED"
    if status == "CANCELLED":
        return "CANCELLED"
    return ""


def build_summary(match: dict, code: str, cfg: dict) -> str:
    home_team = match.get("homeTeam") or {}
    away_team = match.get("awayTeam") or {}
    home, away = display_name(home_team), display_name(away_team)
    # Badges sit on the outside, e.g. "🔴 Arsenal 2–1 Chelsea 🔵"
    home_badge, away_badge = team_badge(home_team, cfg), team_badge(away_team, cfg)
    if home_badge:
        home = f"{home_badge} {home}"
    if away_badge:
        away = f"{away} {away_badge}"
    tag = COMPETITION_TAGS.get(code, code)
    status = match.get("status")
    hs, as_ = score_pair(match)

    if status in ("IN_PLAY", "PAUSED", "FINISHED", "AWARDED") and hs is not None:
        core = f"{home} {hs}–{as_} {away}"
        pens = (match.get("score") or {}).get("penalties") or {}
        if pens.get("home") is not None:
            core += f" ({pens['home']}–{pens['away']} pens)"
    else:
        core = f"{home} v {away}"

    label = live_label(match)
    title = f"{core} · {label}" if label else core
    if cfg.get("show_competition_tag", True):
        title = f"{title} [{tag}]"
    return title


def build_description(match: dict, code: str) -> str:
    # No build timestamp in here: anything that changes on every build makes
    # every event look modified and forces calendar clients to resync them all.
    lines = []
    comp = (match.get("competition") or {}).get("name") or COMPETITION_NAMES.get(code, code)
    lines.append(comp)

    stage = (match.get("stage") or "").replace("_", " ").title()
    matchday = match.get("matchday")
    group = match.get("group")
    if stage and stage.lower() != "regular season":
        lines.append(f"Stage: {stage}")
    if group:
        lines.append(f"Group: {group}")
    if matchday:
        lines.append(f"Matchday {matchday}")

    home_full = (match.get("homeTeam") or {}).get("name") or "TBD"
    away_full = (match.get("awayTeam") or {}).get("name") or "TBD"
    lines.append(f"{home_full} vs {away_full}")

    status = match.get("status")
    lines.append(f"Status: {status}")
    if status == "SCHEDULED":
        lines.append("Kickoff time not confirmed yet")

    hs, as_ = score_pair(match, "fullTime")
    hh, ah = score_pair(match, "halfTime")
    if hs is not None:
        lines.append(f"Score: {hs}-{as_}")
    if hh is not None and status in ("PAUSED", "IN_PLAY", "FINISHED", "AWARDED"):
        lines.append(f"Half time: {hh}-{ah}")
    penalties = (match.get("score") or {}).get("penalties") or {}
    if penalties.get("home") is not None:
        lines.append(f"Penalties: {penalties['home']}-{penalties['away']}")

    venue = match.get("venue")
    if venue:
        lines.append(f"Venue: {venue}")

    match_id = match.get("id")
    if match_id:
        lines.append(f"https://www.football-data.org/match/{match_id}")
    return "\n".join(lines)


def sequence_for(match: dict) -> int:
    """
    Baseline revision number for an event whose content has changed.

    Calendar clients take a changed event more seriously when SEQUENCE grows.
    Status rank moves forward as a match progresses and goals usually
    accumulate, and the day counter keeps it climbing across days. A goal can
    still be taken off by VAR, so build_event also checks against the
    previously published value and never lets SEQUENCE go backwards.
    """
    rank = STATUS_RANK.get(match.get("status"), 0)
    hs, as_ = score_pair(match)
    goals = (hs or 0) + (as_ or 0)
    days = (dt.date.today() - SEQUENCE_EPOCH).days
    return days * 1000 + rank * 100 + min(goals, 99)


def content_hash(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def build_event(match: dict, code: str, cfg: dict, stamp: str,
                previous: dict[str, dict[str, str]] | None = None) -> list[str]:
    kickoff_raw = match.get("utcDate")
    if not kickoff_raw:
        return []
    kickoff = dt.datetime.fromisoformat(kickoff_raw.replace("Z", "+00:00"))
    duration = int(cfg.get("event_duration_minutes", 115))
    if (match.get("score") or {}).get("duration") in ("EXTRA_TIME", "PENALTY_SHOOTOUT"):
        duration += int(cfg.get("extra_time_minutes", 35))
    end = kickoff + dt.timedelta(minutes=duration)

    status = match.get("status")
    if status == "CANCELLED":
        ics_status = "CANCELLED"
    elif status in TENTATIVE_STATUSES:
        ics_status = "TENTATIVE"
    else:
        ics_status = "CONFIRMED"

    body = [
        f"DTSTART:{utc(kickoff)}",
        f"DTEND:{utc(end)}",
        f"STATUS:{ics_status}",
        f"SUMMARY:{esc(build_summary(match, code, cfg))}",
        f"DESCRIPTION:{esc(build_description(match, code))}",
        "TRANSP:TRANSPARENT",
        f"CATEGORIES:{esc(COMPETITION_NAMES.get(code, code))}",
    ]
    venue = match.get("venue")
    if venue:
        body.append(f"LOCATION:{esc(venue)}")

    # Only alert for confirmed kickoff times; a TBC slot would fire at the
    # placeholder time.
    alarm = cfg.get("reminder_minutes_before")
    if alarm and status == "TIMED":
        body += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{esc(build_summary(match, code, cfg))}",
            f"TRIGGER:-PT{int(alarm)}M",
            "END:VALARM",
        ]

    uid = f"{match['id']}@football-calendar"
    digest = content_hash(body)
    sequence = sequence_for(match)
    prev = (previous or {}).get(uid)
    if prev:
        prev_seq = int(prev["SEQUENCE"]) if prev.get("SEQUENCE", "").isdigit() else 0
        if prev.get(HASH_PROP) == digest:
            # Unchanged: republish it byte for byte so clients see no change.
            sequence, stamp = prev_seq, prev.get("DTSTAMP", stamp)
        else:
            sequence = max(sequence, prev_seq + 1)

    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"SEQUENCE:{sequence}",
        f"{HASH_PROP}:{digest}",
        *body,
        "END:VEVENT",
    ]


# --------------------------------------------------------------------------
# Calendar assembly
# --------------------------------------------------------------------------

def build_calendar(events: list[list[str]], name: str, cfg: dict) -> str:
    ttl = int(cfg.get("refresh_minutes", 10))
    head = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{esc(name)}",
        f"X-WR-CALDESC:{esc('Fixtures and live scores. Rebuilt every few minutes.')}",
        f"X-WR-TIMEZONE:{esc(cfg.get('timezone', 'UTC'))}",
        f"X-PUBLISHED-TTL:PT{ttl}M",
        f"REFRESH-INTERVAL;VALUE=DURATION:PT{ttl}M",
    ]
    body = [line for event in events for line in event]
    lines = head + body + ["END:VCALENDAR"]
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def season_for(cfg: dict) -> int:
    season = cfg.get("season", "auto")
    if season != "auto":
        return int(season)
    today = dt.date.today()
    return today.year if today.month >= 7 else today.year - 1


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def check_shrink(fetched: dict[str, int], season: int,
                 previous_status: dict | None) -> list[str]:
    """Competitions whose match count fell sharply since the last build."""
    if not previous_status or previous_status.get("season") != season:
        return []  # nothing to compare, or a new season legitimately resets
    before_all = previous_status.get("competitions") or {}
    problems = []
    for code, count in sorted(fetched.items()):
        before = (before_all.get(code) or {}).get("fetched")
        if before and count < before * SHRINK_LIMIT:
            problems.append(f"{code}: API returned {count} matches, "
                            f"the previous build had {before}")
    return problems


def landing_page(base_url: str, feeds: list[tuple[str, str]]) -> str:
    rows = []
    for label, filename in feeds:
        https = f"{base_url.rstrip('/')}/{filename}"
        webcal = "webcal://" + https.split("://", 1)[-1]
        rows.append(
            f'<li><strong>{html.escape(label)}</strong><br>'
            f'<a href="{html.escape(webcal)}">Subscribe in Apple Calendar</a> &nbsp;·&nbsp; '
            f'<code>{html.escape(https)}</code></li>'
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Football calendar feeds</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 42rem; margin: 3rem auto; padding: 0 1rem; }}
  li {{ margin-bottom: 1.25rem; }}
  code {{ font-size: .85em; word-break: break-all; }}
</style></head>
<body>
<h1>Football calendar feeds</h1>
<p>Subscribe to a feed below. It refreshes on its own; scores appear in the
event titles as matches play out.</p>
<ul>{''.join(rows)}</ul>
<p><small>Data from football-data.org. Last build:
{dt.datetime.now(dt.timezone.utc).strftime('%d %b %Y %H:%M UTC')}</small></p>
</body></html>
"""


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_matches(cfg: dict, token: str, offline: str | None) -> dict[str, list[dict]]:
    if offline:
        payload = json.loads(pathlib.Path(offline).read_text())
        return payload if isinstance(payload, dict) else {"PL": payload}

    season = season_for(cfg)
    result: dict[str, list[dict]] = {}
    for i, code in enumerate(cfg.get("competitions", [])):
        if i:
            time.sleep(7)  # stay comfortably inside 10 requests/minute
        print(f"fetching {code} ({COMPETITION_NAMES.get(code, code)}) season {season}")
        data = api_get(f"/competitions/{code}/matches?season={season}", token)
        matches = data.get("matches")
        if matches is None:
            raise ApiError(f"{code}: response has no 'matches' field")
        print(f"  {len(matches)} matches")
        if not matches:
            print(f"  warning: {code} has no matches for {season} yet", file=sys.stderr)
        result[code] = matches
    return result


def load_venues(cfg: dict, token: str, offline: str | None,
                previous_raw: str | None) -> dict:
    """
    Home ground per team id, as published in venues.json.

    Reuses the previous build's lookup while it is fresh, and never fails the
    build: venues are a nice-to-have, the fixtures are not.
    """
    season = season_for(cfg)
    previous: dict = {}
    if previous_raw:
        try:
            previous = json.loads(previous_raw)
        except ValueError:
            print("  previous venues.json is not valid JSON, ignoring", file=sys.stderr)
    teams = dict(previous.get("teams") or {}) if previous.get("season") == season else {}

    fetched_at = previous.get("fetched_at")
    if teams and fetched_at:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(fetched_at)
        if age < VENUES_MAX_AGE:
            return {"season": season, "fetched_at": fetched_at, "teams": teams}
    if offline:
        return {"season": season, "fetched_at": fetched_at, "teams": teams}

    complete = True
    for code in cfg.get("competitions", []):
        time.sleep(7)  # follows the match requests; stay inside 10/minute
        print(f"fetching {code} home grounds")
        try:
            data = api_get(f"/competitions/{code}/teams?season={season}", token)
        except ApiError as err:
            print(f"  warning: no venues for {code} ({err})", file=sys.stderr)
            complete = False
            continue
        for team in data.get("teams") or []:
            if team.get("id") and team.get("venue"):
                teams[str(team["id"])] = team["venue"]
    if complete:
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    # If any competition failed, keep the old fetched_at so the next build retries.
    return {"season": season, "fetched_at": fetched_at, "teams": teams}


def fill_venues(by_competition: dict[str, list[dict]], venues: dict) -> int:
    """Give each match without a venue its home club's ground. Returns how many."""
    grounds = venues.get("teams") or {}
    filled = 0
    for matches in by_competition.values():
        for match in matches:
            if match.get("venue") or match.get("stage") in NEUTRAL_STAGES:
                continue
            ground = grounds.get(str((match.get("homeTeam") or {}).get("id")))
            if ground:
                match["venue"] = ground
                filled += 1
    return filled


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--out", default="public")
    parser.add_argument("--offline", help="build from a saved JSON payload instead of the API")
    parser.add_argument("--previous",
                        help="URL or directory of the last published build "
                             "(default: base_url when online, none when offline; "
                             "pass '' to disable)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
    if not token and not args.offline:
        sys.exit("FOOTBALL_DATA_TOKEN is not set. Get a free key at "
                 "https://www.football-data.org/client/register")

    base_url = cfg.get("base_url") or os.environ.get("PAGES_BASE_URL", "")
    previous_at = args.previous if args.previous is not None else (
        "" if args.offline else base_url)

    watchlist = {str(t).strip().lower() for t in (cfg.get("teams") or []) if str(t).strip()}
    if watchlist:
        print(f"following {len(watchlist)} team(s): {', '.join(sorted(watchlist))}")
    else:
        print("no teams configured - including every match in each competition")

    # Clubs followed only inside specific competitions, e.g. a side you want in
    # the Champions League but not in their domestic league. Ignored when the
    # global watchlist is empty, since that already means "keep everything".
    only_in = {
        str(code).strip().upper():
            {str(t).strip().lower() for t in (names or []) if str(t).strip()}
        for code, names in (cfg.get("teams_only_in") or {}).items()
    }
    for code, names in sorted(only_in.items()):
        if names and watchlist:
            print(f"{code} only: {', '.join(sorted(names))}")

    try:
        by_competition = load_matches(cfg, token, args.offline)
    except ApiError as err:
        # Publishing without this competition would delete its events from
        # every subscribed calendar, so keep the last good feed instead.
        print(f"error: {err}", file=sys.stderr)
        print("not publishing: the previous feed stays live", file=sys.stderr)
        return 1

    season = season_for(cfg)
    fetched = {code: len(matches) for code, matches in by_competition.items()}
    previous_status = None
    previous_status_raw = read_previous(previous_at, "status.json")
    if previous_status_raw:
        try:
            previous_status = json.loads(previous_status_raw)
        except ValueError:
            print("  previous status.json is not valid JSON, ignoring", file=sys.stderr)
    shrunk = check_shrink(fetched, season, previous_status)
    if shrunk:
        for problem in shrunk:
            print(f"error: {problem}", file=sys.stderr)
        if os.environ.get("ALLOW_SHRINK", "").strip() not in ("", "0", "false"):
            print("ALLOW_SHRINK is set, publishing anyway", file=sys.stderr)
        else:
            print("not publishing: this looks like a partial API response. "
                  "Set ALLOW_SHRINK=1 if the drop is expected.", file=sys.stderr)
            return 1

    if watchlist:
        for warning in watchlist_report(by_competition, watchlist, only_in):
            print(f"warning: {warning}", file=sys.stderr)

    venues = load_venues(cfg, token, args.offline, read_previous(previous_at, "venues.json"))
    print(f"venues: {fill_venues(by_competition, venues)} match(es) given their home ground")

    main_file = cfg.get("filename", "football.ics")
    previous = parse_events(read_previous(previous_at, main_file) or "")
    if previous_at:
        print(f"compared against {len(previous)} previously published event(s)")
    stamp = utc(dt.datetime.now(dt.timezone.utc))

    merged: list[tuple[dt.datetime, list[str]]] = []
    # Every configured competition gets a feed, even an empty one, so its
    # subscribers never hit a 404.
    per_competition: dict[str, list[tuple[dt.datetime, list[str]]]] = {
        code: [] for code in by_competition}
    kept_counts: dict[str, int] = {}

    for code, matches in by_competition.items():
        kept = 0
        effective = watchlist | only_in.get(code, set()) if watchlist else set()
        for match in matches:
            if not matches_watchlist(match, effective):
                continue
            event = build_event(match, code, cfg, stamp, previous)
            if not event:
                continue
            kickoff = dt.datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
            merged.append((kickoff, event))
            per_competition[code].append((kickoff, event))
            kept += 1
        kept_counts[code] = kept
        print(f"{code}: kept {kept} match(es)")

    merged.sort(key=lambda pair: pair[0])

    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    cal_name = cfg.get("calendar_name", "Football")
    (outdir / main_file).write_bytes(
        build_calendar([e for _, e in merged], cal_name, cfg).encode("utf-8"))
    print(f"wrote {outdir / main_file} ({len(merged)} events)")

    feeds = [(f"{cal_name} — all competitions", main_file)]

    if cfg.get("also_write_per_competition", True):
        for code, items in per_competition.items():
            items.sort(key=lambda pair: pair[0])
            label = COMPETITION_NAMES.get(code, code)
            filename = f"{slugify(label)}.ics"
            (outdir / filename).write_bytes(
                build_calendar([e for _, e in items], f"{cal_name}: {label}", cfg).encode("utf-8"))
            feeds.append((label, filename))
            print(f"wrote {outdir / filename} ({len(items)} events)")

    if base_url:
        (outdir / "index.html").write_text(landing_page(base_url, feeds), encoding="utf-8")
        print(f"wrote {outdir / 'index.html'}")

    # Machine-readable build record: the freshness check reads built_at, and
    # the next build compares match counts against it.
    status = {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "season": season,
        "events": len(merged),
        "competitions": {code: {"fetched": fetched[code], "kept": kept_counts[code]}
                         for code in by_competition},
    }
    (outdir / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    # Republished every build: a Pages deploy replaces the whole site.
    (outdir / "venues.json").write_text(
        json.dumps(venues, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    (outdir / ".nojekyll").write_text("", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
