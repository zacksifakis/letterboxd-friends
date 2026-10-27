import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import letterboxd_boxoffice as bo  # noqa: E402

# Box Office Mojo's weekend chart, copied as a table (tab-separated).
MOJO = """Rank\tLW\tRelease\tGross\t%± LW\tTheaters\tChange\tAverage\tTotal Gross\tWeeks\tDistributor
1\t-\tCoyote vs. Acme\t$12,345,678\t-\t3,456\t-\t$3,572\t$12,345,678\t1\tKetchup Entertainment
2\t1\tBlack Phone 2\t$8,000,000\t-48.2%\t3,500\t+12\t$2,285\t$60,000,000\t3\tUniversal Pictures
3\t2\tTron: Ares\t$4,100,000\t-55%\t2,900\t-1,250\t$1,413\t$70,000,000\t4\tWalt Disney
9\t5\tAfter the Hunt\t$900,000\t-60%\t1,100\t-1,200\t$818\t$8,000,000\t3\tAmazon MGM
14\t9\tThe Smashing Machine\t$310,000\t-70%\t600\t-1,400\t$516\t$6,000,000\t5\tA24
"""

# The same chart, copied one cell per line; the top film is in its 2nd week.
CELLS = """1
-
Coyote vs. Acme
$12,345,678
-
3,456
-
$3,572
$20,000,000
2
Ketchup Entertainment
2
1
Black Phone 2
$8,000,000
-48.2%
3,500
+12
$2,285
$60,000,000
3
Universal Pictures
"""

# The Numbers: last week's rank in parentheses, the distributor before the gross,
# copied as plain space-separated text.
NUMBERS = """1 (1) Black Phone 2 Universal $8,000,000 -48% 3,500 +12 $2,285 $60,000,000 3
2 new Coyote vs. Acme Ketchup Entertainment $12,345,678 n/a 3,456 $3,572 $12,345,678 1
"""


class Parse(unittest.TestCase):
    def test_mojo_table(self):
        rows = bo.parse(MOJO)
        self.assertEqual([r["title"] for r in rows],
                         ["Coyote vs. Acme", "Black Phone 2", "Tron: Ares", "After the Hunt",
                          "The Smashing Machine"])
        bp = rows[1]
        self.assertEqual((bp["rank"], bp["gross"], bp["change"], bp["theaters"], bp["thChange"],
                          bp["avg"], bp["total"], bp["weeks"]),
                         (2, 8e6, -48.2, 3500, 12, 2285, 6e7, 3))
        self.assertEqual(rows[2]["thChange"], -1250)
        self.assertEqual(rows[4]["rank"], 14)

    def test_cells(self):
        rows = bo.parse(CELLS)
        self.assertEqual([r["title"] for r in rows], ["Coyote vs. Acme", "Black Phone 2"])
        self.assertEqual(rows[0]["weeks"], 2)
        self.assertEqual(rows[1]["weeks"], 3)

    def test_numbers_text(self):
        rows = bo.parse(NUMBERS)
        self.assertEqual(len(rows), 2)
        self.assertIs(bo.match(rows, "Black Phone 2"), rows[0])
        self.assertIs(bo.match(rows, "Coyote vs. Acme"), rows[1])
        self.assertEqual(rows[0]["weeks"], 3)
        self.assertIsNone(bo.match(rows, "Black Phone"))                       # not the sequel

    def test_shorthand_millions(self):
        r = bo.parse("Coyote vs. Acme  $9.5M  -40%  3,200 theaters  $30M  2")[0]
        self.assertEqual((r["gross"], r["total"], r["change"]), (9.5e6, 3e7, -40.0))


    def test_wrapped_rerelease(self):
        text = ("Domestic 2026 Weekend 43\nOctober 23-25, 2026\n"
                "16\t-\tKlara and the Sun\t$701,059\t-\t880\t-\t$796\t$701,059\t1\tSony Pictures Releasing\n"
                "17\t11\tThe Devils\n2026 Re-release\n"
                "$417,574\t-75.8%\t315\t-35\t$1,325\t$3,146,322\t2\t-\n"
                "18\t14\tYour Mother Your Mother Your Mother\t$397,218\t-65.9%\t710\t-114\t$559\t$3,171,903\t5\tOrion Pictures\n")
        rows = bo.parse(text)
        self.assertEqual([r["title"] for r in rows],
                         ["Klara and the Sun", "The Devils", "Your Mother Your Mother Your Mother"])
        dv = rows[1]
        self.assertEqual((dv["rank"], dv["gross"], dv["theaters"], dv["weeks"]), (17, 417574, 315, 2))
        self.assertIs(bo.match(rows, "Ken Russell\u2019s The Devils"), dv)
        self.assertEqual(bo.weekend(text), "October 23-25, 2026")


class Guess(unittest.TestCase):
    def test_guesses(self):
        rows = {r["title"]: r for r in bo.parse(MOJO)}
        self.assertEqual(bo.guess(rows["Coyote vs. Acme"])[0], "likely")       # opening weekend
        self.assertEqual(bo.guess(rows["Black Phone 2"])[0], "likely")         # $8M, #2
        self.assertEqual(bo.guess(rows["Tron: Ares"])[0], "likely")            # #3, $4.1M
        self.assertEqual(bo.guess(rows["After the Hunt"])[0], "unlikely")      # lost half its theaters
        self.assertEqual(bo.guess(rows["The Smashing Machine"])[0], "unlikely")

    def test_match(self):
        rows = bo.parse(MOJO)
        self.assertEqual(bo.match(rows, "Smashing Machine")["rank"], 14)
        self.assertIsNone(bo.match(rows, "Black Phone"))


class Learn(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.saved = (bo.CACHE, bo.HISTORY, bo.WEEKS)
        bo.CACHE, bo.HISTORY, bo.WEEKS = (os.path.join(self.dir, n) for n in ("c.json", "h.json", "w.json"))
        bo._learned.clear()

    def tearDown(self):
        bo.CACHE, bo.HISTORY, bo.WEEKS = self.saved
        bo._learned.clear()

    def test_two_charts_in_one_paste(self):
        one = "October 16-18, 2026\n" + MOJO
        two = "October 23-25, 2026\n" + MOJO.replace("$8,000,000", "$5,000,000")
        rows, weekends = bo.save(one + "\n" + two)
        self.assertEqual(sorted(weekends), ["2026-10-16", "2026-10-23"])
        self.assertEqual(rows[1]["gross"], 5e6)                    # the newest is the one in use

    def test_examples_and_record(self):
        bo.save("October 16-18, 2026\n" + MOJO)
        bo._save(bo.WEEKS, {
            "2026-10-15": {"end": "2026-10-21", "post": "a", "films": {
                "black-phone-2": {"title": "Black Phone 2", "regular": True},
                "the-smashing-machine": {"title": "The Smashing Machine", "regular": True},
                "tron-ares": {"title": "Tron: Ares", "regular": False}}},          # a one-off: not counted
            "2026-10-22": {"end": "2026-10-28", "post": "b", "films": {
                "black-phone-2": {"title": "Black Phone 2", "regular": True}}}})
        exs = bo.examples()
        self.assertEqual(sorted((r["title"], held) for r, held, _ in exs),
                         [("Black Phone 2", True), ("The Smashing Machine", False)])
        rec = bo.learn()
        self.assertEqual((rec["examples"], rec["rulesRight"], rec["model"]), (2, 2, None))

    def test_model_fits(self):
        import random
        rnd = random.Random(1)
        exs = []
        for i in range(60):
            avg = rnd.uniform(200, 6000)
            exs.append(({"gross": avg * 2000, "weeks": rnd.randint(1, 8), "theaters": 2000,
                         "thChange": 0, "avg": avg}, avg > 1500, str(i % 6)))
        m = bo._model(exs)
        self.assertGreater(bo._prob(m, {"gross": 1e7, "weeks": 2, "theaters": 2000, "thChange": 0, "avg": 5000}), 0.8)
        self.assertLess(bo._prob(m, {"gross": 6e5, "weeks": 6, "theaters": 2000, "thChange": 0, "avg": 300}), 0.2)


if __name__ == "__main__":
    unittest.main()
