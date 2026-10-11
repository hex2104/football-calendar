"""Unit tests for build_ics.py. Standard library only, and runs on Python 3.9.

    python -m unittest discover -s tests
"""

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

import build_ics as b
import validate_ics

CFG = {"reminder_minutes_before": 15, "event_duration_minutes": 115}


def match(status="TIMED", home=None, away=None, mid=1, **extra):
    data = {
        "id": mid,
        "utcDate": "2026-10-18T14:00:00Z",
        "status": status,
        "homeTeam": {"id": 57, "name": "Arsenal FC", "shortName": "Arsenal", "tla": "ARS"},
        "awayTeam": {"id": 61, "name": "Chelsea FC", "shortName": "Chelsea", "tla": "CHE"},
        "score": {"fullTime": {"home": home, "away": away}},
    }
    data.update(extra)
    return data


def props(event):
    return next(iter(b.parse_events("\r\n".join(event)).values()))


class Primitives(unittest.TestCase):
    def test_fold_respects_octets_and_characters(self):
        line = "LOCATION:" + "Estadio Santiago Bernabéu · " * 8
        folded = b.fold(line)
        for physical in folded.split("\r\n"):
            self.assertLessEqual(len(physical.encode("utf-8")), 75)
        self.assertEqual(b.unfold(folded), [line])

    def test_esc(self):
        self.assertEqual(b.esc("a,b;c\\d\ne"), "a\\,b\\;c\\\\d\\ne")


class Watchlist(unittest.TestCase):
    def test_empty_watchlist_keeps_everything(self):
        self.assertTrue(b.matches_watchlist(match(), set()))

    def test_loose_matching(self):
        for want in ("arsenal", "arsenal fc", "ars"):
            self.assertTrue(b.matches_watchlist(match(), {want}))
        self.assertFalse(b.matches_watchlist(match(), {"liverpool"}))

    def test_full_name_avoids_espanyol(self):
        espanyol = {"id": 80, "name": "RCD Espanyol de Barcelona",
                    "shortName": "Espanyol", "tla": "ESP"}
        self.assertFalse(b.team_matches(espanyol, "fc barcelona"))
        self.assertTrue(b.team_matches(espanyol, "barcelona"))

    def test_report_flags_unknown_and_ambiguous_names(self):
        games = {"PL": [match(), match(mid=2, awayTeam={
            "id": 62, "name": "Chelsea Women", "shortName": "Chelsea W", "tla": "CHW"})]}
        warnings = b.watchlist_report(games, {"arsenal", "chelsea", "bayern munich"},
                                      {"CL": {"psg"}})
        text = "\n".join(warnings)
        self.assertIn("'bayern munich' matches no club", text)
        self.assertIn("'chelsea' matches 2 clubs", text)
        self.assertIn("'psg' (in CL) matches no club", text)
        self.assertNotIn("'arsenal'", text)


class Events(unittest.TestCase):
    def test_timed_match_is_confirmed_with_alarm(self):
        event = b.build_event(match("TIMED"), "PL", CFG, "20261011T000000Z")
        self.assertIn("STATUS:CONFIRMED", event)
        self.assertIn("BEGIN:VALARM", event)
        self.assertEqual(props(event)["SUMMARY"], "Arsenal v Chelsea [PL]")

    def test_unconfirmed_kickoff_is_tentative_without_alarm(self):
        event = b.build_event(match("SCHEDULED"), "PL", CFG, "20261011T000000Z")
        self.assertIn("STATUS:TENTATIVE", event)
        self.assertNotIn("BEGIN:VALARM", event)
        self.assertEqual(props(event)["SUMMARY"], "Arsenal v Chelsea · TBC [PL]")

    def test_badges_sit_outside_the_names(self):
        cfg = dict(CFG, team_badges={"Arsenal": "🔴", "Chelsea": "🔵"})
        event = b.build_event(match("FINISHED", 2, 1), "PL", cfg, "20261011T000000Z")
        self.assertEqual(props(event)["SUMMARY"], "🔴 Arsenal 2–1 Chelsea 🔵 · FT [PL]")

    def test_clubs_without_a_badge_get_none(self):
        cfg = dict(CFG, team_badges={"Chelsea": "🔵", "Liverpool": "🔴"})
        event = b.build_event(match(), "PL", cfg, "20261011T000000Z")
        self.assertEqual(props(event)["SUMMARY"], "Arsenal v Chelsea 🔵 [PL]")

    def test_postponed_is_tentative(self):
        event = b.build_event(match("POSTPONED"), "PL", CFG, "20261011T000000Z")
        self.assertIn("STATUS:TENTATIVE", event)
        self.assertNotIn("BEGIN:VALARM", event)

    def test_extra_time_lengthens_event(self):
        aet = match("FINISHED", 2, 1, score={"fullTime": {"home": 2, "away": 1},
                                             "duration": "EXTRA_TIME"})
        event = b.build_event(aet, "CL", CFG, "20261011T000000Z")
        self.assertIn("DTEND:20261018T163000Z", event)  # 115 + 35 minutes
        self.assertEqual(props(event)["SUMMARY"], "Arsenal 2–1 Chelsea · AET [UCL]")

    def test_description_has_no_build_timestamp(self):
        first = b.build_event(match(), "PL", CFG, "20261011T000000Z")
        later = b.build_event(match(), "PL", CFG, "20261011T091500Z")
        self.assertEqual(props(first)[b.HASH_PROP], props(later)[b.HASH_PROP])


class Sequence(unittest.TestCase):
    def previous_for(self, event):
        return b.parse_events("\r\n".join(event))

    def test_unchanged_event_is_republished_identically(self):
        old = b.build_event(match(), "PL", CFG, "20261001T000000Z")
        with mock.patch.object(b, "sequence_for", return_value=10 ** 9):
            new = b.build_event(match(), "PL", CFG, "20261011T000000Z",
                                self.previous_for(old))
        self.assertEqual(old, new)

    def test_disallowed_goal_still_moves_sequence_forward(self):
        goal = b.build_event(match("IN_PLAY", 1, 0), "PL", CFG, "20261011T000000Z")
        var = b.build_event(match("IN_PLAY", 0, 0), "PL", CFG, "20261011T000500Z",
                            self.previous_for(goal))
        self.assertGreater(int(props(var)["SEQUENCE"]), int(props(goal)["SEQUENCE"]))

    def test_previous_feed_without_hash_is_bumped(self):
        old = [line for line in b.build_event(match(), "PL", CFG, "20261001T000000Z")
               if not line.startswith(b.HASH_PROP)]
        old = [f"SEQUENCE:{10 ** 9}" if line.startswith("SEQUENCE:") else line for line in old]
        new = b.build_event(match(), "PL", CFG, "20261011T000000Z", self.previous_for(old))
        self.assertEqual(int(props(new)["SEQUENCE"]), 10 ** 9 + 1)


class ShrinkGuard(unittest.TestCase):
    def test_sharp_drop_in_same_season_is_flagged(self):
        prev = {"season": 2026, "competitions": {"PL": {"fetched": 380}, "CL": {"fetched": 189}}}
        problems = b.check_shrink({"PL": 380, "CL": 40}, 2026, prev)
        self.assertEqual(len(problems), 1)
        self.assertIn("CL", problems[0])

    def test_new_season_or_no_history_is_not_compared(self):
        prev = {"season": 2025, "competitions": {"PL": {"fetched": 380}}}
        self.assertEqual(b.check_shrink({"PL": 0}, 2026, prev), [])
        self.assertEqual(b.check_shrink({"PL": 0}, 2026, None), [])


class Http(unittest.TestCase):
    def http_error(self, code, headers=None):
        return urllib.error.HTTPError("u", code, "x", headers or {}, None)

    def test_server_errors_are_retried(self):
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b'{"matches": []}'
        with mock.patch("urllib.request.urlopen",
                        side_effect=[self.http_error(502), ok]), \
                mock.patch("time.sleep"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.api_get("/x", "t"), {"matches": []})

    def test_exhausted_retries_raise_instead_of_returning_empty(self):
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(429)), \
                mock.patch("time.sleep"), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(b.ApiError):
                b.api_get("/x", "t")

    def test_forbidden_raises(self):
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(403)):
            with self.assertRaises(b.ApiError):
                b.api_get("/x", "t")

    def test_rate_limit_wait_uses_reset_header(self):
        err = self.http_error(429, {"X-RequestCounter-Reset": "12"})
        self.assertEqual(b.retry_wait(err, 0), 13)


class Calendar(unittest.TestCase):
    def test_built_calendar_validates(self):
        events = [b.build_event(match(s, mid=i), "PL", CFG, "20261011T000000Z")
                  for i, s in enumerate(["TIMED", "SCHEDULED", "POSTPONED", "CANCELLED"], 1)]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "f.ics"
            path.write_bytes(b.build_calendar(events, "Test", CFG).encode("utf-8"))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(validate_ics.check(str(path)), 0)

    def test_landing_page_escapes_labels(self):
        page = b.landing_page("https://x.github.io/f", [("<b>Spurs & co</b>", "f.ics")])
        self.assertIn("&lt;b&gt;Spurs &amp; co&lt;/b&gt;", page)


try:
    import yaml  # noqa: F401
except ImportError:  # pragma: no cover
    yaml = None


@unittest.skipIf(yaml is None, "PyYAML not installed")
class OfflineBuild(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parent.parent

    def run_build(self, out, *extra):
        argv = ["build_ics.py", "--config", str(self.ROOT / "config.yaml"),
                "--offline", str(self.ROOT / "sample_payload.json"), "--out", out, *extra]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return b.main()

    def test_rebuild_against_previous_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.assertEqual(self.run_build(first), 0)
            self.assertEqual(self.run_build(second, "--previous", first), 0)
            for feed in sorted(pathlib.Path(first).glob("*.ics")):
                self.assertEqual(feed.read_bytes(), (pathlib.Path(second) / feed.name).read_bytes(),
                                 feed.name)
            status = json.loads((pathlib.Path(second) / "status.json").read_text())
            self.assertIn("built_at", status)

    def test_refuses_to_publish_partial_data(self):
        with tempfile.TemporaryDirectory() as prev, tempfile.TemporaryDirectory() as out:
            self.assertEqual(self.run_build(prev), 0)
            status_path = pathlib.Path(prev) / "status.json"
            status = json.loads(status_path.read_text())
            status["competitions"]["PL"]["fetched"] = 1000
            status_path.write_text(json.dumps(status))
            self.assertEqual(self.run_build(out, "--previous", prev), 1)
            self.assertFalse((pathlib.Path(out) / "football.ics").exists())


if __name__ == "__main__":
    unittest.main()
