"""Matching films on a web page, on a made-up programme. No network access."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import letterboxd_pagefilms as pf  # noqa: E402

FILMS = {"clayface": ("Clayface", "2026", 6), "the-odyssey": ("The Odyssey", "1997", 3),
         "the-odyssey-2026": ("The Odyssey", "2026", 89), "fjord-2026": ("Fjord", "2026", 40),
         "i-was-born-but": ("I Was Born, But...", "1932", 2)}
IX = {"films": FILMS, "tt2slug": {},
      "by_title": {"οδυσσεια": ["the-odyssey", "the-odyssey-2026"], "φιορδ": ["fjord-2026"],
                   "αλλα": ["i-was-born-but"], "clayface": ["clayface"]},
      "recent": [(pf.sounds("Clayface", False), "clayface")]}


class Matching(unittest.TestCase):
    def test_greek_title_picks_the_newest_film(self):
        self.assertEqual(pf.match_line("Οδύσσεια", IX), ("the-odyssey-2026", "greek title"))

    def test_a_printed_year_wins(self):
        self.assertEqual(pf.match_line("Οδύσσεια (1997)", IX)[0], "the-odyssey")

    def test_english_title_spelled_in_greek(self):
        self.assertEqual(pf.match_line("Κλέι Φέις", IX, listing=True), ("clayface", "spelled in Greek"))
        self.assertIsNone(pf.match_line("Κλέι Φέις", IX))        # only trusted in a programme

    def test_ordinary_words_are_not_films(self):
        self.assertIsNone(pf.match_line("Άλλα", IX))              # an old film's title, used as a word
        self.assertIsNone(pf.match_line("(Ντοκιμαντέρ)", IX))


if __name__ == "__main__":
    unittest.main()
