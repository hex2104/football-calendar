#!/usr/bin/env python3
"""Print every club name available in the configured competitions.

Use the output to fill in the `teams:` list in config.yaml.

    export FOOTBALL_DATA_TOKEN=...
    python list_teams.py
"""

import os
import pathlib
import sys
import time

import yaml

from build_ics import API_BASE, COMPETITION_NAMES, api_get, season_for  # noqa: F401


def main() -> int:
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
    if not token:
        sys.exit("FOOTBALL_DATA_TOKEN is not set. Free key: "
                 "https://www.football-data.org/client/register")

    cfg = yaml.safe_load(pathlib.Path("config.yaml").read_text()) or {}
    season = season_for(cfg)

    for code in cfg.get("competitions", []):
        label = COMPETITION_NAMES.get(code, code)
        print(f"\n=== {label} ({code}) — {season}/{str(season + 1)[-2:]} ===")
        data = api_get(f"/competitions/{code}/teams?season={season}", token)
        teams = sorted(data.get("teams", []), key=lambda t: t.get("name", ""))
        if not teams:
            print("  (no data — competition may not be on your plan)")
        for team in teams:
            print(f"  {team.get('tla', '   '):<4} {team.get('shortName', ''):<22} "
                  f"{team.get('name', '')}")
        time.sleep(7)  # free tier allows 10 requests/minute
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
