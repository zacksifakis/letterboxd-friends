"""
Tests for the logic that's easy to get subtly wrong. Everything runs on made-up
data in a temporary folder — never on real lists or real usernames.

    python3 -m unittest discover tests
"""

import csv
import datetime
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import letterboxd_history as history  # noqa: E402
import letterboxd_people as people    # noqa: E402
import letterboxd_posters as posters  # noqa: E402


def write_csv(path, rows):
    fields = ["Film Title", "Year", "Slug", "Friend Watch Count", "Letterboxd URL"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for slug, count in rows:
            w.writerow({"Film Title": slug.title(), "Year": "1999", "Slug": slug,
                        "Friend Watch Count": count, "Letterboxd URL": ""})


def read_counts(path):
    with open(path, newline="") as f:
        return {r["Slug"]: int(r["Friend Watch Count"]) for r in csv.DictReader(f)}


class TopOfListTies(unittest.TestCase):
    """Posters go to a list's top films without ever splitting a tie."""

    def test_short_list_is_taken_whole(self):
        ranked = [("a", 5), ("b", 4), ("c", 3)]
        self.assertEqual(posters.top_slugs(ranked, 10), ["a", "b", "c"])

    def test_cut_falls_between_counts(self):
        ranked = [(f"f{i}", 100 - i) for i in range(30)]
        self.assertEqual(len(posters.top_slugs(ranked, 20)), 20)

    def test_small_tie_at_the_cut_is_kept_whole(self):
        # positions 19-21 share a count; 20 + 20% leaves room for them
        ranked = [(f"f{i}", 100 - i) for i in range(18)] + [("x", 5), ("y", 5), ("z", 5), ("w", 1)]
        top = posters.top_slugs(ranked, 20)
        self.assertEqual(top[-3:], ["x", "y", "z"])
        self.assertNotIn("w", top)

    def test_big_tie_at_the_cut_is_left_out_whole(self):
        ranked = [(f"f{i}", 100 - i) for i in range(15)] + [(f"t{i}", 2) for i in range(40)]
        top = posters.top_slugs(ranked, 20)
        self.assertEqual(len(top), 15)
        self.assertTrue(all(s.startswith("f") for s in top))


class PeopleBase(unittest.TestCase):
    """Points the exclusion code at a scratch home folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self._saved = (people.HOME, people.EXCLUDED, people.SNAPS)
        people.HOME = self.home
        people.EXCLUDED = os.path.join(self.home, "excluded.json")
        people.SNAPS = os.path.join(self.home, "snapshots")

    def tearDown(self):
        people.HOME, people.EXCLUDED, people.SNAPS = self._saved
        self.tmp.cleanup()

    def exclude(self, *users, history_users=()):
        people._save(set(users), history=set(history_users))


class Exclusions(PeopleBase):
    def test_recount_leaves_out_excluded_people(self):
        write_csv(os.path.join(self.home, "friends_watched_1990s.csv"),
                  [("heat", 3), ("fargo", 2), ("clerks", 1)])
        audit = {"alice": ["heat", "fargo"], "bob": ["heat", "clerks"], "carol": ["heat", "fargo"]}
        with open(os.path.join(self.home, "friends_watched_1990s_per_friend.json"), "w") as f:
            json.dump(audit, f)
        self.exclude("bob")
        people.recount_period("1990s")
        counts = read_counts(os.path.join(self.home, "friends_watched_1990s.csv"))
        self.assertEqual(counts, {"heat": 2, "fargo": 2})   # clerks was only bob's: gone

    def test_recount_keeps_the_file_date(self):
        path = os.path.join(self.home, "friends_watched_1990s.csv")
        write_csv(path, [("heat", 2)])
        with open(os.path.join(self.home, "friends_watched_1990s_per_friend.json"), "w") as f:
            json.dump({"alice": ["heat"], "bob": ["heat"]}, f)
        os.utime(path, (1_000_000_000, 1_000_000_000))
        self.exclude("bob")
        people.recount_period("1990s")
        self.assertEqual(os.path.getmtime(path), 1_000_000_000)


class Renames(PeopleBase):
    def test_a_renamed_excluded_account_stays_excluded(self):
        films = {f"film-{i}" for i in range(12)}
        self.exclude("oldname", history_users=["oldname"])
        before = {"oldname": sorted(films), "alice": ["film-0"]}
        now = {"newname": films, "alice": {"film-0"}}
        renamed = people.carry_renames(before, now, log=lambda m: None)
        self.assertEqual(renamed, {"oldname": "newname"})
        # both names stay excluded: lists scraped before the rename still say "oldname"
        self.assertTrue({"oldname", "newname"} <= people.excluded())
        # and they're one person in the history copies — never subtracted twice
        self.assertIn("newname", people._state().get("history", []))

    def test_different_films_are_not_a_rename(self):
        self.exclude("oldname")
        before = {"oldname": [f"a-{i}" for i in range(12)]}
        now = {"stranger": {f"b-{i}" for i in range(12)}}
        self.assertEqual(people.carry_renames(before, now, log=lambda m: None), {})
        self.assertNotIn("stranger", people.excluded())

    def test_too_few_films_to_tell(self):
        self.exclude("oldname")
        before = {"oldname": ["a", "b"]}
        now = {"newname": {"a", "b"}}
        self.assertEqual(people.carry_renames(before, now, log=lambda m: None), {})


class Snapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = history.SNAPS
        history.SNAPS = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, "2026"))

    def tearDown(self):
        history.SNAPS = self._saved
        self.tmp.cleanup()

    def add(self, day):
        path = os.path.join(self.tmp.name, "2026", f"{day.isoformat()}.csv")
        open(path, "w").close()
        return path

    def test_picks_the_newest_copy_on_or_before_the_date(self):
        today = datetime.date.today()
        for back in (1, 6, 9, 20):
            self.add(today - datetime.timedelta(days=back))
        date, _ = history.at_or_before("2026", today - datetime.timedelta(days=7))
        self.assertEqual(date, today - datetime.timedelta(days=9))

    def test_nothing_old_enough(self):
        self.add(datetime.date.today())
        self.assertIsNone(history.at_or_before("2026", datetime.date.today() - datetime.timedelta(days=7)))

    def test_prune_keeps_a_month_daily_then_weekly(self):
        today = datetime.date.today()
        for back in range(0, 60):
            self.add(today - datetime.timedelta(days=back))
        history.prune("2026")
        kept = [d for d, _ in history.snapshots("2026")]
        recent = [d for d in kept if (today - d).days <= history.KEEP_DAILY_DAYS]
        older = [d for d in kept if (today - d).days > history.KEEP_DAILY_DAYS]
        self.assertEqual(len(recent), history.KEEP_DAILY_DAYS + 1)
        weeks = [d.isocalendar()[:2] for d in older]
        self.assertEqual(len(weeks), len(set(weeks)))          # at most one per week


if __name__ == "__main__":
    unittest.main()
