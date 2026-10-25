"""Matching films on a web page, on a made-up programme. No network access."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import letterboxd_pagefilms as pf  # noqa: E402

FILMS = {"poison-1991": ("Poison", "1991", 30), "poison-2024": ("Poison", "2024", 2), "home-2026": ("Home", "2026", 3),
         "clayface": ("Clayface", "2026", 6), "the-odyssey": ("The Odyssey", "1997", 3),
         "the-odyssey-2026": ("The Odyssey", "2026", 89), "fjord-2026": ("Fjord", "2026", 40),
         "i-was-born-but": ("I Was Born, But...", "1932", 2)}
IX = {"films": FILMS, "tt2slug": {},
      "by_title": {"οδυσσεια": ["the-odyssey", "the-odyssey-2026"], "φιορδ": ["fjord-2026"],
                   "αλλα": ["i-was-born-but"], "clayface": ["clayface"],
                   "poison": ["poison-1991", "poison-2024"], "home": ["home-2026"]},
      "recent": [(pf.sounds("Clayface", False), "clayface")]}


class Matching(unittest.TestCase):
    def test_greek_title_picks_the_newest_film(self):
        self.assertEqual(pf.match_line("Οδύσσεια", IX, listing=True), ("the-odyssey-2026", "greek title"))

    def test_a_printed_year_wins(self):
        self.assertEqual(pf.match_line("Οδύσσεια (1997)", IX)[0], "the-odyssey")

    def test_english_title_spelled_in_greek(self):
        self.assertEqual(pf.match_line("Κλέι Φέις", IX, listing=True), ("clayface", "spelled in Greek"))
        self.assertIsNone(pf.match_line("Κλέι Φέις", IX))        # only trusted in a programme

    def test_the_details_line_year_decides(self):
        # "POISON" over "USA | 1991 | 35mm | 85 min." is Haynes's, not the 2024 film
        self.assertEqual(pf.match_line("POISON", IX, listing=True, year="1991")[0], "poison-1991")
        self.assertIsNone(pf.match_line("Clayface", IX, listing=True, year="1966"))

    def test_menu_words_are_not_films(self):
        self.assertIsNone(pf.match_line("Home", IX))
        self.assertIsNone(pf.match_line("Contact", IX))

    def test_ordinary_words_are_not_films(self):
        self.assertIsNone(pf.match_line("Άλλα", IX))              # an old film's title, used as a word
        self.assertIsNone(pf.match_line("(Ντοκιμαντέρ)", IX))


if __name__ == "__main__":
    unittest.main()
