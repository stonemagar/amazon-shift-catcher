import tempfile
import unittest
from pathlib import Path

from amazon_shift_catcher.job_search_browser import (
    WAREHOUSE_TITLE_RE,
    card_requires_detail_location_check,
    detect_application_stage,
    extract_card_location,
    normalise_location_terms,
    text_matches_location,
)
from amazon_shift_catcher.job_search_shifts import (
    apply_time_preference,
    evaluate_shift_opportunity,
    normalise_user_time,
    parse_shift_opportunity,
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

    def test_shift_selection_requires_shift_specific_signal(self):
        class DummyLocator:
            def __init__(self, text):
                self.text = text

            def inner_text(self, timeout=3000):
                return self.text

            def count(self):
                return 0

            def nth(self, index):
                return self

            def is_visible(self):
                return False

            def filter(self, has_text=None):
                return self

        class DummyPage:
            def __init__(self, text):
                self.text = text

            def locator(self, selector):
                return DummyLocator(self.text)

            def get_by_role(self, *args, **kwargs):
                return DummyLocator("")

        normal_job_details = DummyPage(
            "Start date 1 November Shift day Pay rate £15.10 "
            "Hours 20 Location Motherwell"
        )
        real_shift_page = DummyPage(
            "Select work shift Showing 1 of 3 shifts "
            "Start date End date Shift Pay rate Hours Location"
        )

        self.assertEqual(
            detect_application_stage(normal_job_details),
            "UNKNOWN",
        )
        self.assertEqual(
            detect_application_stage(real_shift_page),
            "SHIFT_SELECTION",
        )

    def test_parse_method2_shift_card(self):
        raw = (
            "Start Date: Monday, 16 Oct 2026\n"
            "End Date: Saturday, 16 Jan 2027\n"
            "Shift: Wed, Thu, Fri 9:00 AM - 1:00 PM\n"
            "Pay rate: £15.10/hr\n"
            "Hours: 12 hours per week\n"
            "Location: Motherwell, Scotland"
        )
        opportunity = parse_shift_opportunity(raw)
        self.assertEqual(opportunity.location, "Motherwell, Scotland")
        self.assertEqual(opportunity.hours_per_week, 12)
        self.assertEqual(opportunity.start_time, "09:00")
        self.assertEqual(opportunity.end_time, "13:00")
        self.assertEqual(opportunity.days, ["Wednesday", "Thursday", "Friday"])

    def test_method2_shift_uses_existing_rules_and_location(self):
        opportunity = parse_shift_opportunity(
            "Start Date: 16 Oct 2026\n"
            "End Date: 16 Jan 2027\n"
            "Shift: Wed, Thu, Fri 9:00 AM - 1:00 PM\n"
            "Pay rate: £15.10/hr\n"
            "Hours: 12 hours per week\n"
            "Location: Motherwell, Scotland"
        )
        rules = {
            "max_hours_per_week": 20,
            "allowed_days": ["Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
            "blocked_days": ["Monday", "Tuesday"],
            "allow_night_shift": False,
            "earliest_start_time": "09:00",
            "latest_end_time": "13:00",
            "preferred_hours_order": [20, 12, 8],
        }
        matched, reasons, score = evaluate_shift_opportunity(
            opportunity,
            rules,
            ["Motherwell"],
        )
        self.assertTrue(matched)
        self.assertEqual(reasons, [])
        self.assertGreater(score, 0)

    def test_method2_time_preference_modes(self):
        day_shift = parse_shift_opportunity(
            "Shift: Thu, Fri 9:30 - 14:30\n"
            "Hours: 20hrs per week\n"
            "Location: Motherwell"
        )
        overnight_shift = parse_shift_opportunity(
            "Shift: Fri, Sat 20:00 - 04:00\n"
            "Hours: 20hrs per week\n"
            "Location: Motherwell"
        )
        base = {
            "max_hours_per_week": 20,
            "allowed_days": ["Thursday", "Friday", "Saturday"],
            "blocked_days": [],
            "preferred_hours_order": [20],
            "earliest_start_time": "09:00",
            "latest_end_time": "13:00",
            "allow_night_shift": False,
        }

        any_rules = apply_time_preference(base, "any")
        self.assertTrue(
            evaluate_shift_opportunity(day_shift, any_rules, ["Motherwell"])[0]
        )
        self.assertTrue(
            evaluate_shift_opportunity(overnight_shift, any_rules, ["Motherwell"])[0]
        )

        day_rules = apply_time_preference(base, "day")
        self.assertTrue(
            evaluate_shift_opportunity(day_shift, day_rules, ["Motherwell"])[0]
        )
        self.assertFalse(
            evaluate_shift_opportunity(overnight_shift, day_rules, ["Motherwell"])[0]
        )

        night_rules = apply_time_preference(base, "night")
        self.assertFalse(
            evaluate_shift_opportunity(day_shift, night_rules, ["Motherwell"])[0]
        )
        self.assertTrue(
            evaluate_shift_opportunity(overnight_shift, night_rules, ["Motherwell"])[0]
        )

    def test_method2_specific_time_window_supports_overnight(self):
        overnight_shift = parse_shift_opportunity(
            "Shift: Fri, Sat 22:00 - 04:00\n"
            "Hours: 20hrs per week\n"
            "Location: Motherwell"
        )
        rules = apply_time_preference(
            {
                "max_hours_per_week": 20,
                "allowed_days": ["Friday", "Saturday"],
                "blocked_days": [],
                "preferred_hours_order": [20],
            },
            "window",
            earliest_start="20:00",
            latest_end="06:00",
        )
        matched, reasons, _score = evaluate_shift_opportunity(
            overnight_shift,
            rules,
            ["Motherwell"],
        )
        self.assertTrue(matched)
        self.assertEqual(reasons, [])

    def test_normalise_user_time(self):
        self.assertEqual(normalise_user_time("9 AM"), "09:00")
        self.assertEqual(normalise_user_time("10:30 PM"), "22:30")
        self.assertEqual(normalise_user_time("13:45"), "13:45")
        self.assertIsNone(normalise_user_time("not-a-time"))

    def test_method2_overnight_shift_is_rejected_correctly(self):
        opportunity = parse_shift_opportunity(
            "Start Date: 23 Oct 2026\n"
            "End Date: 16 Jan 2027\n"
            "Shift: Fri, Sat, Sun 15:30 - 0:00\n"
            "Pay rate: £15.10/hr\n"
            "Hours: 20hrs per week\n"
            "Location: Motherwell, Scotland"
        )
        rules = apply_time_preference(
            {
                "max_hours_per_week": 20,
                "allowed_days": ["Friday", "Saturday", "Sunday"],
                "blocked_days": [],
                "preferred_hours_order": [20],
            },
            "day",
        )
        matched, reasons, _score = evaluate_shift_opportunity(
            opportunity,
            rules,
            ["Motherwell"],
        )
        self.assertFalse(matched)
        self.assertIn("not a day shift", reasons)

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
