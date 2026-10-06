import unittest

from amazon_shift_catcher.job_search_browser import (
    WAREHOUSE_TITLE_RE,
    normalise_location_terms,
    text_matches_location,
)


class JobSearchMethodTests(unittest.TestCase):
    def test_location_terms_are_trimmed_and_deduplicated(self):
        self.assertEqual(
            normalise_location_terms(" Wembley, DA1, wembley , Croydon "),
            ["Wembley", "DA1", "Croydon"],
        )

    def test_empty_location_accepts_any_job(self):
        self.assertTrue(
            text_matches_location(
                "Warehouse Associate - Multiple on site locations available",
                [],
            )
        )

    def test_location_matching_is_case_insensitive_and_uses_alternatives(self):
        text = "Warehouse Associate Work address: Dartford, Kent DA1"
        self.assertTrue(text_matches_location(text, ["wembley", "DA1"]))
        self.assertFalse(text_matches_location(text, ["wembley", "croydon"]))

    def test_warehouse_title_accepts_amazon_role_prefixes(self):
        self.assertIsNotNone(WAREHOUSE_TITLE_RE.match("Warehouse Associate"))
        self.assertIsNotNone(
            WAREHOUSE_TITLE_RE.match("Delivery Station Warehouse Associate")
        )
        self.assertIsNotNone(
            WAREHOUSE_TITLE_RE.match("Fulfillment Center Warehouse Associate")
        )
        self.assertIsNone(WAREHOUSE_TITLE_RE.match("Delivery Station Operative"))


if __name__ == "__main__":
    unittest.main()
