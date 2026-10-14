"""
The local-screenings logic, on a made-up newsletter. No network access.

    python3 -m unittest discover tests
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.argv = sys.argv[:1]

import letterboxd_app as app  # noqa: E402

REPORT = """
<p><strong>THIS WEEK (10/8/2026 - 10/14/2026)</strong></p>
<p><em>New Releases</em></p>
<p><a href="https://letterboxd.com/film/heat/">Heat</a> -
   <a href="https://www.amctheatres.com/movies/heat-1">AMC</a>, Marcus Point<br/>
   <a href="https://letterboxd.com/film/fargo/">Fargo</a> -
   <a href="https://flix.example/fargo">7p, 9:45p 10/13 at Flix Brewhouse</a></p>
<p><strong>NEXT WEEK (10/15/2026 - 10/21/2026)</strong></p>
<p><em>Repertory</em></p>
<p><a href="https://letterboxd.com/film/vertigo/">Vertigo</a> - 7p 10/16 at UW Cinematheque</p>
"""


class NewsletterParser(unittest.TestCase):
    def setUp(self):
        self.entries = app._parse_report(REPORT, "https://news.example/p/report", "Screening Report 10/7/2026")

    def by(self, slug):
        return next(e for e in self.entries if e["slug"] == slug)

    def test_finds_every_film_with_its_week(self):
        self.assertEqual({e["slug"] for e in self.entries}, {"heat", "fargo", "vertigo"})
        self.assertEqual(self.by("heat")["start"], "2026-10-08")
        self.assertEqual(self.by("vertigo")["week"], "Next Week")
        self.assertEqual(self.by("vertigo")["section"], "Repertory")

    def test_venues_and_their_links(self):
        heat = self.by("heat")
        self.assertEqual(heat["venues"], ["AMC", "Marcus Point"])
        self.assertEqual(heat["links"], {"AMC": "https://www.amctheatres.com/movies/heat-1"})

    def test_two_times_for_one_date_stay_together(self):
        fargo = self.by("fargo")
        self.assertEqual(fargo["venues"], ["7p, 9:45p 10/13 at Flix Brewhouse"])
        self.assertIn("7p, 9:45p 10/13 at Flix Brewhouse", fargo["links"])


class PlayingNow(unittest.TestCase):
    """A screening drops off once its showtime has passed; an undated run stays."""

    def setUp(self):
        today = datetime.date.today()
        start, end = today - datetime.timedelta(days=3), today + datetime.timedelta(days=3)
        yesterday, tomorrow = today - datetime.timedelta(days=1), today + datetime.timedelta(days=1)
        md = lambda d: f"{d.month}/{d.day}"
        base = {"section": "", "note": "", "week": "This Week", "links": {},
                "start": start.isoformat(), "end": end.isoformat(),
                "post": f"Screening Report {start.month}/{start.day}/{start.year}", "url": ""}
        self.data = {"asOf": "", "latest": None, "entries": [
            dict(base, slug="gone", title="Gone", venues=[f"7p {md(yesterday)} at Somewhere"]),
            dict(base, slug="soon", title="Soon", venues=[f"7p {md(tomorrow)} at Somewhere"]),
            dict(base, slug="running", title="Running", venues=["Marcus Point"]),
        ]}
        self._saved = (app.load_madison, app.get_index)
        app.load_madison = lambda: self.data
        app.get_index = lambda: {}

    def tearDown(self):
        app.load_madison, app.get_index = self._saved

    def test_past_screenings_drop_off(self):
        now = {x["slug"] for x in app.madison_listings()["now"]}
        self.assertEqual(now, {"soon", "running"})

    def test_next_showing_is_reported(self):
        soon = next(x for x in app.madison_listings()["now"] if x["slug"] == "soon")
        self.assertEqual(soon["next"], (datetime.date.today() + datetime.timedelta(days=1)).isoformat())


if __name__ == "__main__":
    unittest.main()
