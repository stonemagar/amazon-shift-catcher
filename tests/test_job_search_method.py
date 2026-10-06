import tempfile
import unittest
from pathlib import Path

from amazon_shift_catcher.job_search_browser import (
    WAREHOUSE_TITLE_RE,
    card_requires_detail_location_check,
    extract_card_location,
    normalise_location_terms,
    text_matches_location,
)
from amazon_shift_catcher.location_storage import load_locations, save_unique_locations


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

    def test_only_multiple_location_cards_require_detail_check(self):
        self.assertTrue(
            card_requires_detail_location_check(
                "Warehouse Associate - Multiple on site locations available"
            )
        )
        self.assertTrue(
            card_requires_detail_location_check(
                "Warehouse Associate - Multiple on-site locations available"
            )
        )
        self.assertFalse(
            card_requires_detail_location_check(
                "Delivery Station Warehouse Associate Motherwell, Scotland"
            )
        )

    def test_extract_card_location_uses_visible_location_line(self):
        raw = (
            "Delivery Station Warehouse Associate\n"
            "Type: Part Time\n"
            "Duration: Fixed-term\n"
            "Pay rate: From £15.10\n"
            "Motherwell, Scotland"
        )
        self.assertEqual(extract_card_location(raw), "Motherwell, Scotland")

    def test_extract_card_location_skips_multiple_location_card(self):
        raw = (
            "Warehouse Associate\n"
            "Type: Part Time\n"
            "Multiple on site locations available"
        )
        self.assertIsNone(extract_card_location(raw))

    def test_unique_location_storage_deduplicates_case_insensitively(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "locations.json"
            first_added = save_unique_locations(
                path,
                ["Motherwell, Scotland", "Dundee, Scotland"],
            )
            second_added = save_unique_locations(
                path,
                ["motherwell, scotland", "Portadown, Northern Ireland"],
            )

            self.assertEqual(
                first_added,
                ["Motherwell, Scotland", "Dundee, Scotland"],
            )
            self.assertEqual(second_added, ["Portadown, Northern Ireland"])
            self.assertEqual(
                load_locations(path),
                [
                    "Motherwell, Scotland",
                    "Dundee, Scotland",
                    "Portadown, Northern Ireland",
                ],
            )

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
