# football-calendar

A self-hosted `.ics` feed of your clubs' fixtures across the Premier League,
La Liga, Bundesliga and Champions League — with the score in the event title as
the match plays out.

```
Tottenham v Arsenal [PL]                 ← before kickoff
Liverpool 1–0 Man City · LIVE 34' [PL]   ← in play
Real Madrid 2–2 Bayern Munich · HT [UCL] ← half time
Arsenal 2–1 Chelsea · FT [PL]            ← full time
```

A GitHub Actions job rebuilds the feed every 10 minutes and publishes it to
GitHub Pages. Apple Calendar subscribes to that URL. Nothing runs on your Mac,
and it costs nothing.

---

## Setup (about 10 minutes)

### 1. Get a free API key

Register at **https://www.football-data.org/client/register**. The key arrives
by email. The free tier covers the four competitions above (plus Serie A,
Ligue 1, Eredivisie, Primeira Liga, Championship and Brasileirão) at 10
requests per minute — this project uses 4 per run.

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
Actions minutes allowance that a 10-minute cron will burn through.

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
  - Bayern Munich
  - Liverpool
```

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
`bundesliga.ics`, `champions-league.ics`.

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
| API → your feed rebuilt | up to 10 min (the cron interval) |
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
`"*/30 * * * *"`.

---

## Things worth knowing

**GitHub's cron is best-effort.** `*/10 * * * *` is a request, not a promise;
runs often lag by a few minutes when the shared runners are busy, and GitHub
drops runs entirely under heavy load. Anything under 5 minutes is ignored.

**Scheduled workflows get switched off after 60 days of repository
inactivity.** GitHub emails you first. Pushing any commit re-enables it. If you
want to avoid thinking about it, push a trivial commit every couple of months,
or run the workflow manually from the Actions tab.

**Free tier score latency.** football-data.org's free plan updates live matches,
but not always as fast as a paid commercial feed. Combined with the polling
above, treat in-play scores as approximate and full-time scores as reliable.

**Season rollover** is automatic: `season: auto` switches to the new season
each July. Pin a year if you want a fixed one.

---

## Files

| File | What it does |
|---|---|
| `build_ics.py` | Fetches matches, filters to your teams, writes the `.ics` files |
| `config.yaml` | Your teams, competitions and calendar preferences |
| `list_teams.py` | Prints every club name the API recognises |
| `validate_ics.py` | RFC 5545 sanity checks (line folding, CRLF, UIDs) |
| `sample_payload.json` | Fake match data for offline testing |
| `.github/workflows/build.yml` | The 10-minute rebuild + Pages deploy |

## Testing locally without an API key

```bash
pip install -r requirements.txt
python build_ics.py --offline sample_payload.json --out /tmp/out
python validate_ics.py /tmp/out/football.ics
```

---

## Implementation notes

**Stable UIDs.** Each event's UID is the football-data.org match ID, so when a
match is rescheduled or the score changes, Apple updates the *existing* event
instead of creating a duplicate.

**Monotonic SEQUENCE.** Calendar clients take an updated event more seriously
when its `SEQUENCE` grows. It's derived from status rank, goals scored and a
day counter, so it only ever climbs across a match's lifecycle:

```
TIMED 2453000 → IN_PLAY 0-0 2453300 → 1-0 2453301 → HT 2453301
      → 1-1 2453302 → FT 2-1 2453503
```

`PAUSED` deliberately shares a rank with `IN_PLAY` so half time doesn't
outrank the second half, and `POSTPONED` shares a rank with `TIMED` so a
rescheduled match doesn't regress.

**Line folding.** Long `DESCRIPTION` values are folded at 75 *octets* on UTF-8
character boundaries, which matters for venue names like *Estadio Santiago
Bernabéu*. Files are written with explicit CRLF line endings.

## Licence

Do whatever you like with it. Match data belongs to football-data.org — check
their terms before redistributing the feed publicly.
