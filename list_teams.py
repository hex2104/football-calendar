#!/usr/bin/env python3
"""Print every club name available in the configured competitions.

Use the output to fill in the `teams:` list in config.yaml.

    export FOOTBALL_DATA_TOKEN=...
    python list_teams.py
"""

import os
import sys
import time

from build_ics import COMPETITION_NAMES, ApiError, api_get, load_config, season_for


def main() -> int:
    token = os.environ.get("FOOTBALL_DATA_TOKEN", "").strip()
    if not token:
        sys.exit("FOOTBALL_DATA_TOKEN is not set. Free key: "
                 "https://www.football-data.org/client/register")

    cfg = load_config("config.yaml")
    season = season_for(cfg)

    for i, code in enumerate(cfg.get("competitions", [])):
        if i:
            time.sleep(7)  # free tier allows 10 requests/minute
        label = COMPETITION_NAMES.get(code, code)
        print(f"\n=== {label} ({code}) — {season}/{str(season + 1)[-2:]} ===")
        try:
            data = api_get(f"/competitions/{code}/teams?season={season}", token)
        except ApiError as err:
            print(f"  (no data — {err})")
            continue
        teams = sorted(data.get("teams", []), key=lambda t: t.get("name", ""))
        if not teams:
            print("  (no teams listed for this season yet)")
        for team in teams:
            print(f"  {team.get('tla', '   '):<4} {team.get('shortName', ''):<22} "
                  f"{team.get('name', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
