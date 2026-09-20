#!/usr/bin/env python3
"""
Build an auto-updating .ics calendar feed of football fixtures and live scores.

Data source: football-data.org v4 (free tier covers PL, La Liga, Bundesliga,
Champions League and more). Set FOOTBALL_DATA_TOKEN in the environment.

Usage:
    python build_ics.py                 # uses config.yaml, writes to ./public
    python build_ics.py --config x.yaml --out ./dist
    python build_ics.py --offline fixture.json   # build from a saved payload (testing)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required:  pip install pyyaml")

API_BASE = "https://api.football-data.org/v4"
PRODID = "-//football-calendar//EN"

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

# SEQUENCE carries a day counter so it still advances if a match is rescheduled
# without its status or scoreline changing.
SEQUENCE_EPOCH = dt.date(2020, 1, 1)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def api_get(path: str, token: str, retries: int = 3) -> dict:
    """GET a football-data.org endpoint, honouring the free tier's rate limit."""
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers={
        "X-Auth-Token": token,
        "User-Agent": "football-calendar/1.0",
    })
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 429:
                # Free tier is 10 requests/minute. Back off and try again.
                wait = 20 * (attempt + 1)
                print(f"  rate limited, sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if err.code in (403, 404):
                print(f"  {url} -> HTTP {err.code} (not on your plan?), skipping",
                      file=sys.stderr)
                return {}
            raise
        except urllib.error.URLError as err:
            if attempt == retries - 1:
                raise
            print(f"  network error ({err}), retrying", file=sys.stderr)
            time.sleep(5)
    return {}


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


def matches_watchlist(match: dict, watchlist: set[str]) -> bool:
    if not watchlist:
        return True  # empty list means "every match in the competition"
    for side in ("homeTeam", "awayTeam"):
        labels = team_labels(match.get(side) or {})
        for want in watchlist:
            if any(want == label or want in label for label in labels):
                return True
    return False


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
    if status == "POSTPONED":
        return "POSTPONED"
    if status == "SUSPENDED":
        return "SUSPENDED"
    if status == "CANCELLED":
        return "CANCELLED"
    return ""


def build_summary(match: dict, code: str, cfg: dict) -> str:
    home = display_name(match.get("homeTeam") or {})
    away = display_name(match.get("awayTeam") or {})
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
    parts = [core]
    if label and label not in ("",):
        parts.append(label)
    title = " · ".join(parts)
    if cfg.get("show_competition_tag", True):
        title = f"{title} [{tag}]"
    return title


def build_description(match: dict, code: str) -> str:
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

    lines.append("")
    lines.append(f"Updated {dt.datetime.now(dt.timezone.utc).strftime('%d %b %Y %H:%M UTC')}")
    return "\n".join(lines)


def sequence_for(match: dict) -> int:
    """
    Monotonically increasing revision number.

    Calendar clients take a changed event more seriously when SEQUENCE grows.
    Status rank moves forward as a match progresses and goals only accumulate,
    so rank*100 + goals never goes backwards within a day; the day counter
    guarantees it keeps climbing across days regardless.
    """
    rank = STATUS_RANK.get(match.get("status"), 0)
    hs, as_ = score_pair(match)
    goals = (hs or 0) + (as_ or 0)
    days = (dt.date.today() - SEQUENCE_EPOCH).days
    return days * 1000 + rank * 100 + min(goals, 99)


def build_event(match: dict, code: str, cfg: dict, stamp: str) -> list[str]:
    kickoff_raw = match.get("utcDate")
    if not kickoff_raw:
        return []
    kickoff = dt.datetime.fromisoformat(kickoff_raw.replace("Z", "+00:00"))
    duration = int(cfg.get("event_duration_minutes", 115))
    end = kickoff + dt.timedelta(minutes=duration)

    status = match.get("status")
    ics_status = "CANCELLED" if status == "CANCELLED" else "CONFIRMED"

    out = [
        "BEGIN:VEVENT",
        f"UID:{match['id']}@football-calendar",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{utc(kickoff)}",
        f"DTEND:{utc(end)}",
        f"SEQUENCE:{sequence_for(match)}",
        f"STATUS:{ics_status}",
        f"SUMMARY:{esc(build_summary(match, code, cfg))}",
        f"DESCRIPTION:{esc(build_description(match, code))}",
        "TRANSP:TRANSPARENT",
        f"CATEGORIES:{esc(COMPETITION_NAMES.get(code, code))}",
    ]
    venue = match.get("venue")
    if venue:
        out.append(f"LOCATION:{esc(venue)}")

    alarm = cfg.get("reminder_minutes_before")
    upcoming = status in ("SCHEDULED", "TIMED")
    if alarm and upcoming:
        out += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{esc(build_summary(match, code, cfg))}",
            f"TRIGGER:-PT{int(alarm)}M",
            "END:VALARM",
        ]
    out.append("END:VEVENT")
    return out


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


def landing_page(base_url: str, feeds: list[tuple[str, str]]) -> str:
    rows = []
    for label, filename in feeds:
        https = f"{base_url.rstrip('/')}/{filename}"
        webcal = "webcal://" + https.split("://", 1)[-1]
        rows.append(
            f'<li><strong>{label}</strong><br>'
            f'<a href="{webcal}">Subscribe in Apple Calendar</a> &nbsp;·&nbsp; '
            f'<code>{https}</code></li>'
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
    for code in cfg.get("competitions", []):
        print(f"fetching {code} ({COMPETITION_NAMES.get(code, code)}) season {season}")
        data = api_get(f"/competitions/{code}/matches?season={season}", token)
        matches = data.get("matches", [])
        print(f"  {len(matches)} matches")
        result[code] = matches
        time.sleep(7)  # stay comfortably inside 10 requests/minute
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--out", default="public")
    parser.add_argument("--offline", help="build from a saved JSON payload instead of the API")
    args = parser.parse_args()

    cfg = yaml.safe_load(pathlib.Path(args.config).read_text()) or {}
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
    if not token and not args.offline:
        sys.exit("FOOTBALL_DATA_TOKEN is not set. Get a free key at "
                 "https://www.football-data.org/client/register")

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

    by_competition = load_matches(cfg, token, args.offline)
    stamp = utc(dt.datetime.now(dt.timezone.utc))

    merged: list[tuple[dt.datetime, list[str]]] = []
    per_competition: dict[str, list[tuple[dt.datetime, list[str]]]] = {}

    for code, matches in by_competition.items():
        kept = 0
        effective = watchlist | only_in.get(code, set()) if watchlist else set()
        for match in matches:
            if not matches_watchlist(match, effective):
                continue
            event = build_event(match, code, cfg, stamp)
            if not event:
                continue
            kickoff = dt.datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
            merged.append((kickoff, event))
            per_competition.setdefault(code, []).append((kickoff, event))
            kept += 1
        print(f"{code}: kept {kept} match(es)")

    merged.sort(key=lambda pair: pair[0])

    outdir = pathlib.Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    cal_name = cfg.get("calendar_name", "Football")
    main_file = cfg.get("filename", "football.ics")
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

    base_url = cfg.get("base_url") or os.environ.get("PAGES_BASE_URL", "")
    if base_url:
        (outdir / "index.html").write_text(landing_page(base_url, feeds), encoding="utf-8")
        print(f"wrote {outdir / 'index.html'}")

    (outdir / ".nojekyll").write_text("", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
