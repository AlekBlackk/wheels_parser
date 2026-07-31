"""История находок в SQLite (wheelsparser.db)."""

import json
import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

from tests.dbfixture import use_temp_db
from wheelsparser import db
from wheelsparser.timeutils import now_msk


class DbTestCase(unittest.TestCase):
    """Каждый тест получает свою пустую базу во временном каталоге.

    init_db здесь не вызывается: тесты миграции должны увидеть базу до
    первого init_db, остальные классы создают схему сами.
    """

    def setUp(self):
        self.tmp = use_temp_db(self, init=False)
        self.legacy = self.tmp / "freebets.json"
        self.now = now_msk()

    def at(self, minutes_ago: float) -> str:
        return (self.now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")

    def wheel(self, url="https://betboom.ru/freestream/a", **overrides):
        entry = {
            "url": url,
            "found_at": self.at(1),
            "channel": "demo",
            "msg_id": "demo/1",
            "message_url": "https://t.me/demo/1",
            "preview": "новое колесо",
            "edited": False,
            "status": "active",
            "referral": False,
            "ends_at": "",
            "notified": True,
        }
        entry.update(overrides)
        return entry


class RoundTripTests(DbTestCase):
    def test_wheel_entry_survives_round_trip(self):
        db.init_db()
        entry = self.wheel(referral=True, ends_at=self.at(-30), edited=True)

        db.insert_entries([entry])
        (stored,) = db.entries_since(self.now - timedelta(hours=1))

        for field in ("url", "channel", "msg_id", "message_url", "preview", "status"):
            self.assertEqual(stored[field], entry[field])
        self.assertEqual(stored["found_at"], entry["found_at"])
        self.assertEqual(stored["ends_at"], entry["ends_at"])
        self.assertIs(stored["referral"], True)
        self.assertIs(stored["edited"], True)
        self.assertIs(stored["notified"], True)

    def test_keyword_entry_keeps_keywords_and_has_no_url(self):
        db.init_db()
        entry = {
            "found_at": self.at(1),
            "channel": "demo",
            "msg_id": "demo/7",
            "message_url": "https://t.me/demo/7",
            "preview": "будет колесо",
            "preview_html": "будет <b>колесо</b>",
            "keywords": ["колесо", "*розыгрыш*"],
            "notified": False,
        }

        db.insert_entries([entry])
        (stored,) = db.entries_since(self.now - timedelta(hours=1))

        self.assertEqual(stored["keywords"], ["колесо", "*розыгрыш*"])
        self.assertEqual(stored["preview_html"], entry["preview_html"])
        self.assertFalse(stored.get("url"))
        self.assertIs(stored["notified"], False)

    def test_twitch_entry_keeps_source_and_roles(self):
        db.init_db()
        entry = self.wheel(source="twitch", author="bot", author_roles=["moderator", "bot"])

        db.insert_entries([entry])
        (stored,) = db.entries_since(self.now - timedelta(hours=1))

        self.assertEqual(stored["source"], "twitch")
        self.assertEqual(stored["author"], "bot")
        self.assertEqual(stored["author_roles"], ["moderator", "bot"])

    def test_insert_assigns_growing_ids_to_entries(self):
        db.init_db()
        first, second = self.wheel(url="https://x/1"), self.wheel(url="https://x/2")

        db.insert_entries([first, second])

        self.assertLess(first["id"], second["id"])

    def test_init_db_is_idempotent(self):
        db.init_db()
        db.insert_entries([self.wheel()])
        db.init_db()

        self.assertEqual(db.total_wheels(), 1)


class WindowQueryTests(DbTestCase):
    def setUp(self):
        super().setUp()
        db.init_db()

    def test_entries_since_drops_older_records(self):
        db.insert_entries([
            self.wheel(url="https://x/fresh", found_at=self.at(5)),
            self.wheel(url="https://x/stale", found_at=self.at(120)),
        ])

        fresh = db.entries_since(self.now - timedelta(minutes=30))

        self.assertEqual([item["url"] for item in fresh], ["https://x/fresh"])

    def test_entries_since_returns_oldest_first(self):
        db.insert_entries([
            self.wheel(url="https://x/second", found_at=self.at(2)),
            self.wheel(url="https://x/first", found_at=self.at(9)),
        ])

        found = db.entries_since(self.now - timedelta(minutes=30))

        self.assertEqual(
            [item["url"] for item in found], ["https://x/first", "https://x/second"]
        )

    def test_wheels_since_skips_keyword_entries(self):
        db.insert_entries([
            self.wheel(url="https://x/wheel"),
            {"found_at": self.at(1), "channel": "demo", "keywords": ["колесо"]},
        ])

        wheels = db.wheels_since(self.now - timedelta(minutes=30))

        self.assertEqual([item["url"] for item in wheels], ["https://x/wheel"])


class PendingRetryTests(DbTestCase):
    def setUp(self):
        super().setUp()
        db.init_db()

    def test_returns_only_undelivered_entries(self):
        db.insert_entries([
            self.wheel(url="https://x/sent", notified=True),
            self.wheel(url="https://x/failed", notified=False),
            self.wheel(url="https://x/unknown", notified=False, delivery_unknown=True),
        ])

        pending = db.pending_retry(self.now - timedelta(minutes=180), 10)

        self.assertEqual([item["url"] for item in pending], ["https://x/failed"])

    def test_entries_older_than_window_are_skipped(self):
        db.insert_entries([self.wheel(notified=False, found_at=self.at(400))])

        self.assertEqual(db.pending_retry(self.now - timedelta(minutes=180), 10), [])

    def test_limit_caps_the_batch(self):
        db.insert_entries([
            self.wheel(url=f"https://x/{index}", notified=False) for index in range(5)
        ])

        self.assertEqual(len(db.pending_retry(self.now - timedelta(minutes=180), 2)), 2)

    def test_records_without_url_or_keywords_do_not_take_limit_slots(self):
        # Отправлять по такой записи нечего. Попади она в выборку — заняла
        # бы место настоящей находки и та не ретраилась бы никогда.
        db.insert_entries([
            {"found_at": self.at(1), "channel": "demo", "notified": False},
            self.wheel(url="https://x/real", notified=False),
        ])

        pending = db.pending_retry(self.now - timedelta(minutes=180), 1)

        self.assertEqual([item["url"] for item in pending], ["https://x/real"])


class UpdateDeliveryTests(DbTestCase):
    def setUp(self):
        super().setUp()
        db.init_db()

    def test_persists_notified_flag(self):
        entry = self.wheel(notified=False)
        db.insert_entries([entry])

        entry["notified"] = True
        db.update_delivery(entry)

        self.assertEqual(db.pending_retry(self.now - timedelta(minutes=180), 10), [])

    def test_persists_delivery_unknown_flag(self):
        entry = self.wheel(notified=False)
        db.insert_entries([entry])

        entry["delivery_unknown"] = True
        db.update_delivery(entry)

        self.assertEqual(db.pending_retry(self.now - timedelta(minutes=180), 10), [])

    def test_entry_without_id_is_ignored(self):
        # Записи, не пришедшие из базы (например, из тестов или очереди
        # twitch до вставки), обновлять нечего — это не ошибка.
        db.update_delivery({"url": "https://x/1", "notified": True})

        self.assertEqual(db.total_wheels(), 0)


class PruneTests(DbTestCase):
    def test_prune_keeps_newest_records(self):
        db.init_db()
        db.insert_entries([
            self.wheel(url=f"https://x/{index}", found_at=self.at(10 - index))
            for index in range(5)
        ])

        removed = db.prune(2)
        kept = [item["url"] for item in db.entries_since(self.now - timedelta(hours=1))]

        self.assertEqual(removed, 3)
        self.assertEqual(kept, ["https://x/3", "https://x/4"])

    def test_prune_under_limit_removes_nothing(self):
        db.init_db()
        db.insert_entries([self.wheel()])

        self.assertEqual(db.prune(100), 0)


class StatsTests(DbTestCase):
    def setUp(self):
        super().setUp()
        db.init_db()

    def test_counts_total_today_and_last_wheel(self):
        # Полдень, а не «сейчас»: иначе тест, запущенный после полуночи,
        # уводил бы «два часа назад» во вчерашние сутки.
        noon = self.now.replace(hour=12, minute=0, second=0, microsecond=0)

        def ago(hours):
            return (noon - timedelta(hours=hours)).isoformat(timespec="seconds")

        db.insert_entries([
            self.wheel(url="https://x/old", found_at=ago(24)),
            self.wheel(url="https://x/early", found_at=ago(2)),
            self.wheel(url="https://x/last", found_at=ago(1)),
            {"found_at": ago(1), "channel": "demo", "keywords": ["колесо"]},
        ])

        with patch.object(db, "now_msk", return_value=noon):
            stats = db.wheel_stats()

        # Записи по ключевым словам колёсами не считаются.
        self.assertEqual(stats.total, 3)
        self.assertEqual(stats.today, 2)
        self.assertEqual(stats.last["url"], "https://x/last")

    def test_empty_database_has_no_last_wheel(self):
        stats = db.wheel_stats()

        self.assertEqual((stats.total, stats.today), (0, 0))
        self.assertIsNone(stats.last)

    def test_channel_counts_rank_by_number_of_wheels(self):
        db.insert_entries([
            self.wheel(url="https://x/1", channel="often"),
            self.wheel(url="https://x/2", channel="often"),
            self.wheel(url="https://x/3", channel="rare"),
            {"found_at": self.at(1), "channel": "words", "keywords": ["колесо"]},
        ])

        counts = db.channel_counts(self.now - timedelta(days=30))

        self.assertEqual(counts[0].channel, "often")
        self.assertEqual(counts[0].wheels, 2)
        self.assertEqual([row.channel for row in counts], ["often", "rare"])

    def test_channel_counts_separate_twitch_from_telegram(self):
        db.insert_entries([
            self.wheel(url="https://x/1", channel="demo"),
            self.wheel(url="https://x/2", channel="demo", source="twitch"),
        ])

        counts = db.channel_counts(self.now - timedelta(days=30))

        self.assertEqual(
            sorted((row.source, row.wheels) for row in counts),
            [("telegram", 1), ("twitch", 1)],
        )


class ConcurrencyTests(DbTestCase):
    """С базой одновременно работают четыре потока: parser, bot,
    twitch-worker и active-api. Каждый открывает своё соединение —
    sqlite3.Connection принадлежит создавшему его потоку."""

    def setUp(self):
        super().setUp()
        db.init_db()

    def test_parallel_writers_do_not_lose_records(self):
        errors: list[BaseException] = []

        def writer(prefix):
            try:
                for index in range(25):
                    db.insert_entries([self.wheel(url=f"https://x/{prefix}-{index}")])
            except BaseException as error:
                errors.append(error)
            finally:
                db.close_connection()

        threads = [
            threading.Thread(target=writer, args=(name,))
            for name in ("parser", "twitch", "bot")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(db.total_wheels(), 75)

    def test_reader_thread_works_while_another_writes(self):
        # Ради этого включён WAL: /status и /active не должны падать с
        # «database is locked», пока цикл парсера пишет находки.
        errors: list[BaseException] = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    db.wheel_stats()
            except BaseException as error:
                errors.append(error)
            finally:
                db.close_connection()

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for index in range(50):
                db.insert_entries([self.wheel(url=f"https://x/{index}")])
        finally:
            stop.set()
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(db.total_wheels(), 50)


class LegacyMigrationTests(DbTestCase):
    def test_imports_records_from_freebets_json(self):
        self.legacy.write_text(
            json.dumps([self.wheel(url="https://x/legacy")], ensure_ascii=False),
            encoding="utf-8",
        )

        db.init_db()

        (stored,) = db.entries_since(self.now - timedelta(hours=1))
        self.assertEqual(stored["url"], "https://x/legacy")

    def test_migrated_file_is_renamed_so_import_runs_once(self):
        self.legacy.write_text(json.dumps([self.wheel()]), encoding="utf-8")

        db.init_db()

        self.assertFalse(self.legacy.exists())
        self.assertTrue(self.legacy.with_suffix(".json.migrated").exists())

    def test_naive_timestamps_are_normalized_to_msk(self):
        # Старые версии писали found_at без смещения — в базе метки должны
        # быть в одном формате, иначе сравнение окон ломается.
        naive = self.now.replace(tzinfo=None).isoformat(timespec="seconds")
        self.legacy.write_text(
            json.dumps([self.wheel(url="https://x/naive", found_at=naive)]),
            encoding="utf-8",
        )

        db.init_db()

        (stored,) = db.entries_since(self.now - timedelta(hours=1))
        self.assertEqual(
            stored["found_at"], self.now.isoformat(timespec="seconds")
        )

    def test_corrupt_legacy_file_is_set_aside_instead_of_retried(self):
        # Иначе нечитаемый файл ронял бы предупреждение при каждом старте.
        self.legacy.write_text("{не json", encoding="utf-8")

        db.init_db()

        self.assertEqual(db.total_wheels(), 0)
        self.assertFalse(self.legacy.exists())
        self.assertTrue(self.legacy.with_suffix(".json.broken").exists())


if __name__ == "__main__":
    unittest.main()
