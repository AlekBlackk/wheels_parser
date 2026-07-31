"""Кулдаун повторных уведомлений и его восстановление из базы."""

import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.dbfixture import use_temp_db
from wheelsparser import alerts, db
from wheelsparser.timeutils import now_msk


class SeedFromHistoryTests(unittest.TestCase):
    """После рестарта кулдаун живёт только в памяти — его нужно поднять
    из базы, иначе та же ссылка уйдёт в Telegram повторно раньше срока."""

    def setUp(self):
        use_temp_db(self)
        patcher = patch.dict(alerts.LAST_URL_ALERT, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = now_msk()

    def store(self, url, minutes_ago):
        db.insert_entries([{
            "url": url,
            "found_at": (self.now - timedelta(minutes=minutes_ago)).isoformat(
                timespec="seconds"
            ),
            "channel": "demo",
            "notified": True,
        }])

    def test_recent_finding_starts_the_cooldown(self):
        self.store("https://betboom.ru/freestream/fresh", minutes_ago=1)

        alerts.seed_url_alerts_from_history()

        self.assertTrue(
            alerts.cooldown_active("https://betboom.ru/freestream/fresh", self.now)
        )

    def test_findings_older_than_cooldown_are_not_loaded(self):
        self.store(
            "https://betboom.ru/freestream/stale",
            minutes_ago=alerts.REALERT_COOLDOWN_MINUTES + 5,
        )

        alerts.seed_url_alerts_from_history()

        self.assertEqual(alerts.LAST_URL_ALERT, {})

    def test_url_with_query_tail_is_canonicalized(self):
        # Записи, перенесённые из freebets.json, могли сохранить utm-хвост:
        # без канонизации кулдаун не узнал бы то же самое колесо.
        self.store("https://www.betboom.ru/freestream/one/?utm_source=tg", minutes_ago=1)

        alerts.seed_url_alerts_from_history()

        self.assertTrue(
            alerts.cooldown_active("https://betboom.ru/freestream/one", self.now)
        )

    def test_keyword_records_are_ignored(self):
        db.insert_entries([{
            "found_at": self.now.isoformat(timespec="seconds"),
            "channel": "demo",
            "keywords": ["колесо"],
        }])

        alerts.seed_url_alerts_from_history()

        self.assertEqual(alerts.LAST_URL_ALERT, {})


if __name__ == "__main__":
    unittest.main()
