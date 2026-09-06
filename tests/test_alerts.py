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


class ClaimUrlAlertTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(alerts.LAST_URL_ALERT, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = now_msk()

    def test_claim_succeeds_once_and_blocks_subsequent(self):
        url = "https://betboom.ru/freestream/claim_test"
        self.assertTrue(alerts.claim_url_alert(url, self.now))
        # Второй вызов внутри кулдауна отклонён
        self.assertFalse(alerts.claim_url_alert(url, self.now))
        self.assertTrue(alerts.cooldown_active(url, self.now))

    def test_claim_after_cooldown_expires_succeeds(self):
        url = "https://betboom.ru/freestream/claim_expire"
        self.assertTrue(alerts.claim_url_alert(url, self.now))
        future = self.now + timedelta(minutes=alerts.REALERT_COOLDOWN_MINUTES + 1)
        self.assertTrue(alerts.claim_url_alert(url, future))

    def test_release_allows_reclaiming(self):
        url = "https://betboom.ru/freestream/claim_release"
        self.assertTrue(alerts.claim_url_alert(url, self.now))
        self.assertFalse(alerts.claim_url_alert(url, self.now))

        alerts.release_url_alert(url)
        self.assertFalse(alerts.cooldown_active(url, self.now))
        # После release можно занять снова
        self.assertTrue(alerts.claim_url_alert(url, self.now))

    def test_concurrent_claims_only_one_succeeds(self):
        import concurrent.futures

        url = "https://betboom.ru/freestream/concurrent_race"
        results: list[bool] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(alerts.claim_url_alert, url, self.now) for _ in range(8)]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 7)

    def test_claim_respects_last_found_snapshot(self):
        url = "https://betboom.ru/freestream/claim_snapshot"
        last_found = {url: self.now}
        # Если URL уже есть в last_found снимка цикла, claim отклонён
        self.assertFalse(alerts.claim_url_alert(url, self.now, last_found))
        self.assertNotIn(url, alerts.LAST_URL_ALERT)

        # Но если запись в last_found старше кулдауна, claim успешен
        stale_last_found = {
            url: self.now - timedelta(minutes=alerts.REALERT_COOLDOWN_MINUTES + 1)
        }
        self.assertTrue(alerts.claim_url_alert(url, self.now, stale_last_found))
        self.assertEqual(alerts.LAST_URL_ALERT[url], self.now)

    def test_release_with_claimed_at_protects_concurrent_overwrites(self):
        url = "https://betboom.ru/freestream/safe_release"
        t1 = self.now
        t2 = self.now + timedelta(seconds=10)

        self.assertTrue(alerts.claim_url_alert(url, t1))
        # Другой поток занял с более свежей меткой
        alerts.LAST_URL_ALERT[url] = t2

        # Попытка освободить со старой меткой t1 не должна сбросить метку t2
        alerts.release_url_alert(url, claimed_at=t1)
        self.assertEqual(alerts.LAST_URL_ALERT.get(url), t2)

        # Освобождение с правильной меткой t2 успешно сбрасывает запись
        alerts.release_url_alert(url, claimed_at=t2)
        self.assertNotIn(url, alerts.LAST_URL_ALERT)


if __name__ == "__main__":
    unittest.main()
