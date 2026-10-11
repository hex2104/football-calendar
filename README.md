# football-calendar

A self-hosted `.ics` feed of your clubs' fixtures across the Premier League,
La Liga, Bundesliga and Champions League — with the score in the event title as
the match plays out.

```
Brentford v Arsenal 🔴 · TBC [PL]        ← kickoff time not confirmed yet
Tottenham v Arsenal 🔴 [PL]              ← before kickoff
Liverpool 1–0 Man City · LIVE 34' [PL]   ← in play
⚪ Real Madrid 2–2 Bayern 🔴 · HT [UCL]   ← half time
🔴 Arsenal 2–1 Chelsea 🔵 · FT [PL]      ← full time
```

A GitHub Actions job rebuilds the feed and publishes it to GitHub Pages, and
Apple Calendar subscribes to that URL. It costs nothing. GitHub's own cron is
unreliable, so for timely scores you'll also want an external trigger (see
[Triggering builds reliably](#triggering-builds-reliably)).

---

## Setup (about 10 minutes)

### 1. Get a free API key

Register at **https://www.football-data.org/client/register**. The key arrives
by email. The free tier covers the four competitions above (plus Serie A,
Ligue 1, Eredivisie, Primeira Liga, Championship and Brasileirão) at 10
requests per minute. This project uses 4 per run.

### 2. Create the repository

```bash
git init football-calendar && cd football-calendar
# copy these files in
git add . && git commit -m "Football calendar feed"
gh repo create football-calendar --public --source=. --push
```

A **public** repo keeps GitHub Actions free and unlimited. The feed contains
nothing secret — your API key lives in an encrypted secret, not in the code.
If you'd rather keep it private, note that private repos have a monthly
Actions minutes allowance that a 10-minute cron will burn through. The build
also reads the currently published feed back from Pages, so the Pages site
must be reachable.

### 3. Add your API key as a secret

Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `FOOTBALL_DATA_TOKEN` | the key from your email |

### 4. Turn on GitHub Pages

Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**

### 5. Pick your teams

Edit `config.yaml`. Names match loosely against the club's full name, short
name and three-letter code, so `Arsenal`, `arsenal fc` and `ARS` all work.

```yaml
teams:
  - Arsenal
  - Real Madrid
  - Bayern        # the API calls them "FC Bayern München", so not "Bayern Munich"
  - Liverpool
```

Every club in an event title gets a colour dot from its main colour, which
the API provides ("Red / White" → 🔴). Opponents get one too, but which
matches appear is still decided by `teams`. To change a club's dot, map it
under `team_badges` (`Arsenal: 🔴`). Set `auto_badges: false` to show dots
only for the clubs you map. Calendar apps can't show real club crests in a
subscribed feed, so emoji are the closest you can get.

Each build prints a warning for any name that matches no club, or more than
one (a bare `Barcelona` also matches Espanyol). Check the build log after
changing the list.

To see exactly what the API calls each club:

```bash
pip install -r requirements.txt
export FOOTBALL_DATA_TOKEN=your_key_here
python list_teams.py
```

Leave `teams:` empty to pull every match in every configured competition
(~1,400 events a season — usually more than you want).

### 6. Run it

Repo → **Actions → Build football calendar → Run workflow**.

When it finishes, your feed is at:

```
https://<your-username>.github.io/football-calendar/football.ics
```

Per-competition feeds sit alongside it: `premier-league.ics`, `la-liga.ics`,
`bundesliga.ics`, `champions-league.ics`. Each configured competition always
gets a file, even when none of your teams play in it, so a subscription never
breaks. `status.json` records when the feed was last built, and
`clubs.json` caches each club's home ground and colours.

Optionally put that base URL into `config.yaml` as `base_url` and push — the
next build generates an `index.html` with one-click `webcal://` links.

---

## Subscribing in Apple Calendar

### On a Mac (recommended — this is where you control the refresh rate)

1. Calendar → **File → New Calendar Subscription…**
2. Paste the URL, swapping `https://` for `webcal://`:
   `webcal://<your-username>.github.io/football-calendar/football.ics`
3. **Location:** choose **iCloud** so it syncs to your iPhone and iPad.
   Choose *On My Mac* if you only want it on this machine.
4. **Auto-refresh:** set to **Every 5 minutes**.
5. Untick *Remove alerts* if you want the 15-minute kickoff reminder.

To change the refresh rate later: right-click the calendar in the sidebar →
**Get Info**.

### On an iPhone or iPad (if you have no Mac)

**Settings → Apps → Calendar → Calendar Accounts → Add Account → Other →
Add Subscribed Calendar**, then paste the `https://` URL.

---

## How fast do scores actually appear?

This is the honest limitation, and it's Apple's, not this project's.

| Step | Typical delay |
|---|---|
| Goal scored → football-data.org | seconds to a minute |
| API → your feed rebuilt | up to 10 min with a reliable trigger; GitHub's cron alone can be much slower |
| Feed → Apple Calendar on macOS | 5 min, if you set auto-refresh to 5 min |
| Feed → Apple Calendar on iPhone | **roughly hourly** — not user-configurable |

So on a Mac with a 5-minute refresh you'll typically see a goal within ~15
minutes. On an iPhone, a subscribed calendar refreshes on iOS's own schedule,
which is around once an hour and can't be tightened per calendar.

**What this means in practice:** the calendar is excellent for fixtures,
kickoff times, rescheduling and *final* scores. It is not a substitute for
FotMob's push notifications if you want a goal alert within seconds — no
calendar-based approach can be, because calendar clients poll rather than
receive pushes. If live minute-by-minute is what you're after, keep FotMob for
that and use this for everything else.

If you only care about final scores, set `reminder_minutes_before: null` and
change the cron in `.github/workflows/build.yml` to something gentler like
`"4,34 * * * *"`.

---

## Things worth knowing

**Scheduled workflows get switched off after 60 days of repository
inactivity.** `keepalive.yml` re-enables them every week, so you don't need to
push commits to keep them running.

**Venues are the home club's ground.** The free tier leaves the venue out of
match data, so each event's location is the home team's stadium, taken from
the API's team list. It's cached in `clubs.json` and refreshed weekly, so
normal builds make no extra API calls. Finals are left blank because they're
played at neutral grounds. A match moved to a temporary ground will still show
the club's usual stadium.

**Unconfirmed kickoff times are marked tentative.** Leagues publish dates long
before TV picks fix the kickoff times. Until then the event is `TENTATIVE`,
gets "TBC" in its title and has no alert, because the time is a placeholder.
Postponed matches are also tentative.

**Free tier score latency.** football-data.org's free plan updates live matches,
but not always as fast as a paid commercial feed. Combined with the polling
above, treat in-play scores as approximate and full-time scores as reliable.

**Season rollover** is automatic: `season: auto` switches to the new season
each July. Pin a year if you want a fixed one.

**A failed fetch never wipes your calendar.** If the API errors, or a
competition suddenly returns less than half the matches it did in the previous
build of the same season, the build fails and the last good feed stays live.
If the drop is expected, run the workflow manually with **allow_shrink** ticked.

---

## Triggering builds reliably

GitHub's cron is best-effort. The 10-minute schedule in `build.yml` is a
request, not a promise: runs often lag when the shared runners are busy, and
GitHub drops queued runs entirely under heavy load. In practice it can go an
hour or more without firing.

For timely updates, run an external scheduler that calls
`gh workflow run build.yml` (a `workflow_dispatch`). Any always-on machine
works, e.g. a launchd agent on a Mac or a cron job on a server. The repo cron
then stays as a fallback.

`freshness.yml` checks `status.json` every hour. If the last build is more than
3 hours old, it starts a build and fails, so GitHub emails you. If you get that
email, check your external trigger first.

---

## Files

| File | What it does |
|---|---|
| `build_ics.py` | Fetches matches, filters to your teams, writes the `.ics` files |
| `config.yaml` | Your teams, competitions and calendar preferences |
| `list_teams.py` | Prints every club name the API recognises |
| `validate_ics.py` | RFC 5545 sanity checks (line folding, CRLF, UIDs, STATUS) |
| `sample_payload.json` | Fake match data for offline testing |
| `tests/` | Unit tests (standard library only) |
| `.github/workflows/build.yml` | The 10-minute rebuild + Pages deploy |
| `.github/workflows/test.yml` | Tests and an offline build on Python 3.9 and 3.12 |
| `.github/workflows/freshness.yml` | Hourly check that the feed isn't stale |
| `.github/workflows/keepalive.yml` | Stops GitHub disabling the schedules |

## Testing locally without an API key

```bash
pip install -r requirements.txt
python -m unittest discover -s tests
python build_ics.py --offline sample_payload.json --out /tmp/out
python validate_ics.py /tmp/out/*.ics
```

The code targets Python 3.9 and later. CI runs it on both 3.9 and 3.12.

---

## Implementation notes

**Stable UIDs.** Each event's UID is the football-data.org match ID, so when a
match is rescheduled or the score changes, Apple updates the *existing* event
instead of creating a duplicate.

**Only changed events change.** Each event carries a hash of its content
(`X-FOOTBALL-CALENDAR-HASH`). Each build fetches the currently published feed.
An event whose hash hasn't changed is republished exactly as before, with the
same `SEQUENCE` and `DTSTAMP`, so calendar clients don't resync events nobody
touched. Event descriptions deliberately contain no build timestamp.

**Monotonic SEQUENCE.** Calendar clients take an updated event more seriously
when its `SEQUENCE` grows. For a changed event, the baseline is derived from
status rank, goals scored and a day counter:

```
TIMED 2453000 → IN_PLAY 0-0 2453300 → 1-0 2453301 → HT 2453301
      → 1-1 2453302 → FT 2-1 2453503
```

The new value is also always at least one more than the previously published
`SEQUENCE`. That covers the cases the formula can't, like a goal ruled out by
VAR.

**Line folding.** Long `DESCRIPTION` values are folded at 75 *octets* on UTF-8
character boundaries, which matters for venue names like *Estadio Santiago
Bernabéu*. Files are written with explicit CRLF line endings.

## Licence

Do whatever you like with it. Match data belongs to football-data.org — check
their terms before redistributing the feed publicly.
