import tempfile
import unittest
from datetime import date
from pathlib import Path

from amazon_shift_catcher.release_intelligence import (
    LondonFilter,
    ReleaseHistory,
    TelegramRelease,
    parse_telegram_export,
    parse_telegram_page,
    summarise_releases,
)


SAMPLE_HTML = """
<html><body>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="amazonukjobsalert/1001">
    <div class="tgme_widget_message_text">
      📍 Barking, England (Bow) IG11 0HQ<br/>
      🏷️ Delivery Station Warehouse Associate | 1<br/>
      💼 Seasonal | Part-time<br/>
      💰 £16.10 /hr<br/>
      📅 First Day: 2026-10-03<br/>
      ⏰ Schedule: Sat, Sun, Mon, Tue, Wed 15:15 - 20:15<br/>
      🕐 Hours/Week: 25<br/>
      🔗 https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000206<br/>
      ❌ No longer available (was live for 6 seconds)
    </div>
    <time datetime="2026-09-25T04:53:00+00:00"></time>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="amazonukjobsalert/1002">
    <div class="tgme_widget_message_text">
      📍 North Ferriby, England (Hull Area) HU14 3FT<br/>
      🏷️ Warehouse Associate | 1<br/>
      💼 Seasonal | Reduced<br/>
      💰 £15.10 /hr<br/>
      📅 First Day: 2026-09-30<br/>
      ⏰ Schedule: Wed, Thu, Fri, Sat 18:30 - 03:00<br/>
      🕐 Hours/Week: 30<br/>
      🔗 https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=JOB-UK-0000000349<br/>
      ❌ No longer available (was live for 1 min)
    </div>
    <time datetime="2026-09-25T04:58:00+00:00"></time>
  </div>
</div>
<a class="tme_messages_more" href="/s/amazonukjobsalert?before=1001">
  Older
</a>
</body></html>
"""


class ReleaseIntelligenceTests(unittest.TestCase):
    def test_parse_telegram_page(self):
        releases, older = parse_telegram_page(SAMPLE_HTML)

        self.assertEqual(len(releases), 2)
        barking = releases[0]
        self.assertEqual(barking.message_id, "1001")
        self.assertEqual(barking.postcode, "IG11 0HQ")
        self.assertEqual(barking.job_id, "JOB-UK-0000000206")
        self.assertEqual(barking.hours_per_week, 25.0)
        self.assertEqual(barking.live_seconds, 6)
        self.assertIn("before=1001", older or "")

    def test_parse_telegram_export(self):
        export = {
            "name": "UK Amazon warehouse jobs alert",
            "type": "public_channel",
            "messages": [
                {
                    "id": 58046,
                    "type": "message",
                    "date": "2026-09-01T00:21:24",
                    "text": [
                        "📍 London, England (West London) NW10 0UP\n"
                        "🏷️ Delivery Station Warehouse Associate | 1\n"
                        "💼 Seasonal | Reduced\n"
                        "💰 £15.30 ",
                        {"type": "bot_command", "text": "/hr"},
                        "\n📅 First Day: 2026-09-13\n"
                        "⏰ Schedule: Sun, Mon, Tue, Wed, Thu 14:00 - 20:30\n"
                        "🕐 Hours/Week: 30\n\n🔗 ",
                        {
                            "type": "link",
                            "text": (
                                "https://www.jobsatamazon.co.uk/app#/jobDetail"
                                "?jobId=JOB-UK-0000000212"
                            ),
                        },
                        "\n\n❌ No longer available (was live for 1h 15m)",
                    ],
                },
                {
                    "id": 58047,
                    "type": "message",
                    "date": "2026-08-31T23:59:59",
                    "text": "older message",
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            export_path = Path(tmp) / "result.json"
            export_path.write_text(
                __import__("json").dumps(export),
                encoding="utf-8",
            )
            releases = parse_telegram_export(
                export_path,
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 25),
            )

        self.assertEqual(len(releases), 1)
        release = releases[0]
        self.assertEqual(release.message_id, "58046")
        self.assertEqual(release.postcode, "NW10 0UP")
        self.assertEqual(release.job_id, "JOB-UK-0000000212")
        self.assertEqual(release.hours_per_week, 30.0)
        self.assertEqual(release.live_seconds, 4500)

    def test_london_filter_uses_lookup_for_outer_postcodes(self):
        decisions = {
            "IG11 0HQ": True,
            "HU14 3FT": False,
        }

        with tempfile.TemporaryDirectory() as tmp:
            london_filter = LondonFilter(
                Path(tmp) / "postcode-cache.json",
                lookup=lambda postcode: decisions.get(postcode, False),
            )
            releases, _ = parse_telegram_page(SAMPLE_HTML)
            filtered = london_filter.filter(releases)

        self.assertEqual(
            [item.job_id for item in filtered],
            ["JOB-UK-0000000206"],
        )

    def test_history_deduplicates_same_message_id(self):
        release = TelegramRelease(
            message_id="1",
            observed_at="2026-09-25T01:00:00+00:00",
            location="London, England (West London)",
            postcode="NW10 0UP",
            job_id="JOB-UK-0000000212",
            schedule="Mon 09:00 - 13:00",
            hours_per_week=20,
        )
        later = TelegramRelease(
            message_id="2",
            observed_at="2026-09-25T03:00:00+00:00",
            location="London, England (West London)",
            postcode="NW10 0UP",
            job_id="JOB-UK-0000000212",
            schedule="Mon 09:00 - 13:00",
            hours_per_week=20,
        )

        with tempfile.TemporaryDirectory() as tmp:
            history = ReleaseHistory(Path(tmp) / "history.json")
            saved = history.merge([release, release, later])

        self.assertEqual(len(saved), 2)

    def test_summary_counts_events_days_and_unique_schedules(self):
        releases = [
            TelegramRelease(
                message_id="1",
                observed_at="2026-09-24T01:00:00+00:00",
                location="London, England (West London)",
                postcode="NW10 0UP",
                job_id="JOB-UK-0000000212",
                schedule="Mon 09:00 - 13:00",
                hours_per_week=20,
                live_seconds=5,
            ),
            TelegramRelease(
                message_id="2",
                observed_at="2026-09-25T03:00:00+00:00",
                location="London, England (West London)",
                postcode="NW10 0UP",
                job_id="JOB-UK-0000000212",
                schedule="Mon 09:00 - 13:00",
                hours_per_week=20,
                live_seconds=7,
            ),
        ]

        summaries = summarise_releases(
            releases,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 30),
        )

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].observed_release_events, 2)
        self.assertEqual(summaries[0].active_days, 2)
        self.assertEqual(summaries[0].unique_schedules, 1)
        self.assertEqual(summaries[0].median_live_seconds, 6.0)


if __name__ == "__main__":
    unittest.main()
