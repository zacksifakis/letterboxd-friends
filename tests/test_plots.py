import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import letterboxd_plots as lp  # noqa: E402

FILMS = {"new": ("New", "2026", 5), "old-big": ("Old", "1950", 40), "old-small": ("Old", "1930", 3),
         "en-only": ("E", "2025", 9)}
EL = "Μια γυναίκα επιστρέφει στο χωριό της μετά από χρόνια και βρίσκει το σπίτι της άδειο, ενώ οι γείτονες αρνούνται να της πουν τι συνέβη."
ENG = "A woman returns to her village after many years and finds her house empty while the neighbours refuse to say what happened."


class Pick(unittest.TestCase):
    def test_new_films_then_greek_backlog_then_english(self):
        got = lp.pick(list(FILMS), FILMS, {"en-only"}, set(), {}, 10, 2026)
        self.assertEqual(got, [("new", ["el", "en"]), ("old-big", ["el"]), ("old-small", ["el"]),
                               ("en-only", ["en"])])

    def test_cap_and_skipped(self):
        got = lp.pick(list(FILMS), FILMS, {"en-only"}, set(), {"new": ["el", "en"]}, 1, 2026)
        self.assertEqual(got, [("old-big", ["el"])])


class Merge(unittest.TestCase):
    def test_saves_good_and_sets_aside_empty(self):
        el, en, skipped = {}, {}, {}
        batch = [{"slug": "a", "write": ["el", "en"]}, {"slug": "b", "write": ["el"]}]
        saved, aside, bad = lp.merge(batch, {"a": {"el": EL, "en": ENG}, "b": None}, el, en, skipped)
        self.assertEqual((saved, aside, bad), (2, 1, []))
        self.assertEqual(skipped, {"b": ["el"]})

    def test_never_replaces_and_rejects_wrong_language(self):
        el, en = {"a": "παλιό"}, {}
        saved, _, bad = lp.merge([{"slug": "a", "write": ["el", "en"]}], {"a": {"el": EL, "en": EL}}, el, en, {})
        self.assertEqual((saved, el["a"], en), (0, "παλιό", {}))
        self.assertEqual(len(bad), 1)


if __name__ == "__main__":
    unittest.main()
