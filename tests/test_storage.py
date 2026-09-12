import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from wheelsparser import registry, storage


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wheelsparser-storage-"))


class EnsureDataDirTests(TempDirTestCase):
    def _patch_paths(self, root: Path, data: Path):
        return [
            patch.object(storage, "BASE_DIR", root),
            patch.object(storage, "DATA_DIR", data),
            patch.object(storage, "OUTPUT_FILE", data / "freebets.json"),
            patch.object(storage, "SEEN_FILE", data / "seen_ids.json"),
            patch.object(storage, "BOT_STATE_FILE", data / "bot_state.json"),
            patch.object(storage, "REMOVED_WHEELS_FILE", data / "removed_wheels.json"),
            patch.object(storage, "LOG_FILE", data / "parser.log"),
        ]

    def test_moves_legacy_files_from_root(self):
        root = self.tmp
        data = root / "data"
        (root / "seen_ids.json").write_text("{}", encoding="utf-8")
        (root / "parser.log").write_text("old log", encoding="utf-8")
        (root / "parser.log.1").write_text("rotated", encoding="utf-8")

        patchers = self._patch_paths(root, data)
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        moved = storage.ensure_data_dir()

        self.assertEqual(
            sorted(moved), ["parser.log", "parser.log.1", "seen_ids.json"]
        )
        self.assertTrue((data / "seen_ids.json").exists())
        self.assertTrue((data / "parser.log.1").exists())
        self.assertFalse((root / "seen_ids.json").exists())

    def test_does_not_overwrite_existing_target(self):
        root = self.tmp
        data = root / "data"
        data.mkdir()
        (root / "seen_ids.json").write_text('{"legacy": {}}', encoding="utf-8")
        (data / "seen_ids.json").write_text('{"current": {}}', encoding="utf-8")

        patchers = self._patch_paths(root, data)
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        moved = storage.ensure_data_dir()

        self.assertEqual(moved, [])
        self.assertEqual(
            (data / "seen_ids.json").read_text(encoding="utf-8"), '{"current": {}}'
        )

    def test_creates_data_dir_when_nothing_to_migrate(self):
        root = self.tmp
        data = root / "data"
        patchers = self._patch_paths(root, data)
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.assertEqual(storage.ensure_data_dir(), [])
        self.assertTrue(data.is_dir())


class JsonHelpersTests(TempDirTestCase):
    def test_read_json_returns_default_for_missing_file(self):
        self.assertEqual(storage.read_json(self.tmp / "absent.json", []), [])

    def test_read_json_returns_default_for_corrupt_file(self):
        path = self.tmp / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(storage.read_json(path, {"a": 1}), {"a": 1})

    def test_atomic_write_round_trip(self):
        path = self.tmp / "data.json"
        storage.atomic_write_json(path, {"ключ": "значение"})
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")), {"ключ": "значение"}
        )
        self.assertFalse(path.with_suffix(".json.tmp").exists())


class SeenStateTests(TempDirTestCase):
    def test_load_seen_upgrades_legacy_list_format(self):
        seen_file = self.tmp / "seen_ids.json"
        seen_file.write_text(
            json.dumps({"demo": ["demo/1", "demo/2"], "new": {"new/3": "hash"}}),
            encoding="utf-8",
        )
        with patch.object(storage, "SEEN_FILE", seen_file), \
             patch.object(registry, "CHANNELS", ["demo", "new", "fresh"]):
            seen, has_state = storage.load_seen()

        self.assertTrue(has_state)
        self.assertEqual(seen["demo"], {"demo/1": "", "demo/2": ""})
        self.assertEqual(seen["new"], {"new/3": "hash"})
        self.assertEqual(seen["fresh"], {})

    def test_load_seen_without_file_reports_no_state(self):
        with patch.object(storage, "SEEN_FILE", self.tmp / "absent.json"), \
             patch.object(registry, "CHANNELS", ["demo"]):
            seen, has_state = storage.load_seen()
        self.assertFalse(has_state)
        self.assertEqual(seen, {"demo": {}})

    def test_save_seen_trims_oldest_ids_and_drops_removed_channels(self):
        seen_file = self.tmp / "seen_ids.json"
        seen = {
            "demo": {f"demo/{index}": "" for index in range(1, 6)},
            "removed": {"removed/1": ""},
        }
        with patch.object(storage, "SEEN_FILE", seen_file), \
             patch.object(storage, "MAX_SEEN_PER_CHANNEL", 3), \
             patch.object(registry, "CHANNELS", ["demo"]):
            storage.save_seen(seen)

        stored = json.loads(seen_file.read_text(encoding="utf-8"))
        self.assertEqual(list(stored), ["demo"])
        self.assertEqual(list(stored["demo"]), ["demo/3", "demo/4", "demo/5"])
        # Из памяти удалённый канал не выбрасывается.
        self.assertIn("removed", seen)

    def test_message_id_sort_key_is_numeric(self):
        ids = ["demo/999", "demo/1000", "demo/2"]
        self.assertEqual(
            sorted(ids, key=storage.message_id_sort_key),
            ["demo/2", "demo/999", "demo/1000"],
        )


class RemovedWheelsTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.file = self.tmp / "removed_wheels.json"
        patcher = patch.object(storage, "REMOVED_WHEELS_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Сбрасываем ленивый кэш: каждый тест загружает файл заново.
        storage.REMOVED_WHEELS = None
        self.addCleanup(setattr, storage, "REMOVED_WHEELS", None)

    def test_mark_wheel_removed_persists_and_deduplicates(self):
        with patch.object(storage, "today_msk", return_value="2026-07-30"):
            self.assertTrue(storage.mark_wheel_removed("https://x/one"))
            self.assertFalse(storage.mark_wheel_removed("https://x/one"))
            self.assertEqual(storage.removed_wheels_today(), {"https://x/one"})
        self.assertEqual(
            json.loads(self.file.read_text(encoding="utf-8")),
            {"https://x/one": "2026-07-30"},
        )

    def test_yesterdays_removals_are_pruned(self):
        self.file.write_text(
            json.dumps({"https://x/old": "2026-07-29", "https://x/new": "2026-07-30"}),
            encoding="utf-8",
        )
        with patch.object(storage, "today_msk", return_value="2026-07-30"):
            self.assertEqual(storage.removed_wheels_today(), {"https://x/new"})

    def test_unmark_wheel_removed_restores_and_reports(self):
        with patch.object(storage, "today_msk", return_value="2026-07-30"):
            storage.mark_wheel_removed("https://x/one")
            self.assertTrue(storage.unmark_wheel_removed("https://x/one"))
            self.assertEqual(storage.removed_wheels_today(), set())
            self.assertFalse(storage.unmark_wheel_removed("https://x/one"))
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8")), {})


class PendingExpiredStateTests(TempDirTestCase):
    """pending_expired.json — единственный способ пережить рестарт для
    PENDING_EXPIRED_RETRY (см. parser.py): пост, чья ссылка ошибочно
    признана expired, уже помечен «увиденным» в seen_ids.json, и без этого
    файла рестарт терял бы такую находку навсегда."""

    def setUp(self):
        super().setUp()
        self.file = self.tmp / "pending_expired.json"
        patcher = patch.object(storage, "PENDING_EXPIRED_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_without_file_returns_empty(self):
        self.assertEqual(storage.load_pending_expired(), {})

    def test_save_and_load_round_trip(self):
        first_seen = storage.parse_msk("2026-07-30T12:00:00+03:00")
        pending = {
            "https://betboom.ru/freestream/a": {
                "channel": "demo",
                "msg_id": "demo/1",
                "message_url": "https://t.me/demo/1",
                "preview": "колесо",
                "post_text": "колесо",
                "first_seen": first_seen,
            }
        }
        storage.save_pending_expired(pending)
        self.assertEqual(storage.load_pending_expired(), pending)

    def test_load_drops_entries_with_unparsable_first_seen(self):
        self.file.write_text(
            json.dumps({
                "https://x/bad": {"channel": "demo", "first_seen": "not-a-date"},
                "https://x/good": {
                    "channel": "demo",
                    "msg_id": "1",
                    "message_url": "u",
                    "preview": "p",
                    "post_text": "t",
                    "first_seen": "2026-07-30T12:00:00+03:00",
                },
            }),
            encoding="utf-8",
        )
        loaded = storage.load_pending_expired()
        self.assertNotIn("https://x/bad", loaded)
        self.assertIn("https://x/good", loaded)

    def test_save_does_not_raise_on_disk_failure(self):
        # Сбой записи не должен ронять цикл парсинга — только лог.
        with patch.object(
            storage, "atomic_write_json", side_effect=OSError("disk full")
        ):
            storage.save_pending_expired({})


class SuggestedChannelsTests(TempDirTestCase):
    """Предложение добавить канал-первоисточник делается один раз на канал
    и переживает рестарт: молчание админа — тоже ответ (см.
    parser.suggest_forward_source)."""

    def setUp(self):
        super().setUp()
        self.path = self.tmp / "suggested_channels.json"
        patcher = patch.object(storage, "SUGGESTED_CHANNELS_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        reset = patch.object(storage, "SUGGESTED_CHANNELS", set())
        reset.start()
        self.addCleanup(reset.stop)

    def test_first_suggestion_is_allowed_and_second_is_not(self):
        self.assertTrue(storage.mark_channel_suggested("origchannel"))
        self.assertFalse(storage.mark_channel_suggested("origchannel"))

    def test_different_channels_are_tracked_separately(self):
        self.assertTrue(storage.mark_channel_suggested("first"))
        self.assertTrue(storage.mark_channel_suggested("second"))
        self.assertEqual(
            sorted(json.loads(self.path.read_text(encoding="utf-8"))),
            ["first", "second"],
        )

    def test_state_is_restored_from_disk_after_restart(self):
        storage.mark_channel_suggested("origchannel")

        # Рестарт: состояние в памяти сброшено (ленивая загрузка), файл цел.
        with patch.object(storage, "SUGGESTED_CHANNELS", None):
            self.assertFalse(storage.mark_channel_suggested("origchannel"))

    def test_broken_file_does_not_crash_loading(self):
        self.path.write_text("{ не json", encoding="utf-8")
        with patch.object(storage, "SUGGESTED_CHANNELS", None):
            self.assertTrue(storage.mark_channel_suggested("origchannel"))

    def test_write_failure_still_allows_the_suggestion(self):
        # Сбой диска не должен «съесть» находку: предложение уходит,
        # просто без гарантии пережить рестарт.
        with patch.object(storage, "atomic_write_json", side_effect=OSError("disk")):
            self.assertTrue(storage.mark_channel_suggested("origchannel"))


class BotOffsetTests(TempDirTestCase):
    def test_offset_round_trip(self):
        path = self.tmp / "bot_state.json"
        with patch.object(storage, "BOT_STATE_FILE", path):
            self.assertEqual(storage.load_bot_offset(), 0)
            storage.save_bot_offset(42)
            self.assertEqual(storage.load_bot_offset(), 42)

    def test_negative_or_junk_offset_becomes_zero(self):
        path = self.tmp / "bot_state.json"
        path.write_text(json.dumps({"offset": "junk"}), encoding="utf-8")
        with patch.object(storage, "BOT_STATE_FILE", path):
            self.assertEqual(storage.load_bot_offset(), 0)


class StreamerFrontierTests(TempDirTestCase):
    def test_frontier_round_trip_with_empty_scans(self):
        path = self.tmp / "streamers.json"
        with patch.object(storage, "STREAMERS_FILE", path):
            self.assertEqual(storage.load_streamer_frontier(), {})
            data = {
                "valid_series": {"index": 10, "width": 2, "pending": [11], "empty_scans": 3},
            }
            storage.save_streamer_frontier(data)
            loaded = storage.load_streamer_frontier()
            self.assertEqual(loaded["valid_series"]["index"], 10)
            self.assertEqual(loaded["valid_series"]["width"], 2)
            self.assertEqual(loaded["valid_series"]["pending"], [11])
            self.assertEqual(loaded["valid_series"]["empty_scans"], 3)

    def test_series_at_its_base_survives_a_restart(self):
        # Голый адрес серии — width 0. Пока загрузчик требовал width >= 1,
        # такая серия молча исчезала при перезапуске.
        path = self.tmp / "streamers.json"
        with patch.object(storage, "STREAMERS_FILE", path):
            storage.save_streamer_frontier({"nix": {"index": 0, "width": 0, "pending": []}})

            loaded = storage.load_streamer_frontier()

            self.assertEqual(loaded["nix"], {"index": 0, "width": 0, "pending": []})

    def test_negative_width_is_still_rejected(self):
        path = self.tmp / "streamers.json"
        with patch.object(storage, "STREAMERS_FILE", path):
            path.write_text(
                json.dumps({"nix": {"index": 0, "width": -1, "pending": []}}),
                encoding="utf-8",
            )

            self.assertEqual(storage.load_streamer_frontier(), {})

    def test_invalid_prefix_regex_filtered_on_load_and_save(self):
        path = self.tmp / "streamers.json"
        with patch.object(storage, "STREAMERS_FILE", path):
            data = {
                "valid_name": {"index": 5, "width": 1, "pending": []},
                "bad!name": {"index": 5, "width": 1, "pending": []},
                "a": {"index": 5, "width": 1, "pending": []},
                "toolongprefixwithmorethanthirtytwocharactersinit": {
                    "index": 5, "width": 1, "pending": [],
                },
            }
            storage.save_streamer_frontier(data)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("valid_name", raw)
            self.assertNotIn("bad!name", raw)
            self.assertNotIn("a", raw)
            self.assertNotIn("toolongprefixwithmorethanthirtytwocharactersinit", raw)

            loaded = storage.load_streamer_frontier()
            self.assertIn("valid_name", loaded)
            self.assertNotIn("bad!name", loaded)


class RetiredSeriesTests(TempDirTestCase):
    def test_retired_series_round_trip(self):
        path = self.tmp / "retired_streamers.json"
        with patch.object(storage, "RETIRED_STREAMERS_FILE", path):
            self.assertEqual(storage.load_retired_series(), {})
            data = {"dead_streamer": 15, "invalid!name": 20, "valid_2": 8}
            storage.save_retired_series(data)
            loaded = storage.load_retired_series()
            self.assertEqual(loaded, {"dead_streamer": 15, "valid_2": 8})

    def test_retirement_expires_after_the_configured_window(self):
        # Отставка навсегда означала, что серия, притихшая на неделю,
        # не вернётся никогда. На проде так потерялось 29 серий из 35.
        path = self.tmp / "retired_streamers.json"
        stale = (storage.now_msk() - timedelta(days=storage.PREDICTIVE_RETIRE_DAYS + 1))
        with patch.object(storage, "RETIRED_STREAMERS_FILE", path):
            path.write_text(
                json.dumps({
                    "long_gone": {"index": 15, "retired_at": stale.isoformat()},
                    "just_retired": {
                        "index": 4, "retired_at": storage.now_msk().isoformat()
                    },
                }),
                encoding="utf-8",
            )

            loaded = storage.load_retired_series()

            self.assertNotIn("long_gone", loaded)
            self.assertEqual(loaded["just_retired"], 4)

    def test_legacy_plain_index_is_treated_as_expired(self):
        # Старый формат — голое число без даты. Срок по нему не восстановить,
        # а политика сменилась, поэтому такие серии возвращаются в перебор.
        path = self.tmp / "retired_streamers.json"
        with patch.object(storage, "RETIRED_STREAMERS_FILE", path):
            path.write_text(json.dumps({"legacy": 9}), encoding="utf-8")

            self.assertEqual(storage.load_retired_series(), {})


class WatchedWheelsTests(TempDirTestCase):
    """Наблюдатель помнит, какой розыгрыш он последним видел по каждому
    адресу: сменился action_uid — значит стартовал новый."""

    def test_round_trip(self):
        path = self.tmp / "watched_wheels.json"
        with patch.object(storage, "WATCHED_WHEELS_FILE", path):
            self.assertEqual(storage.load_watched_uids(), {})
            storage.save_watched_uids({"https://betboom.ru/freestream/over": "uid-1"})

            self.assertEqual(
                storage.load_watched_uids(),
                {"https://betboom.ru/freestream/over": "uid-1"},
            )

    def test_non_string_entries_are_dropped(self):
        path = self.tmp / "watched_wheels.json"
        with patch.object(storage, "WATCHED_WHEELS_FILE", path):
            path.write_text(
                json.dumps({"https://x/one": "uid", "https://x/two": 42, "3": None}),
                encoding="utf-8",
            )

            self.assertEqual(storage.load_watched_uids(), {"https://x/one": "uid"})


if __name__ == "__main__":
    unittest.main()
