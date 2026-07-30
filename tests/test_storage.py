import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
