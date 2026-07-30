import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wheelsparser import registry


class ParseHelpersTests(unittest.TestCase):
    def test_parse_channels_strips_and_deduplicates(self):
        self.assertEqual(
            registry.parse_channels(" @one, two ,one,,"), ["one", "two"]
        )

    def test_parse_twitch_channels_lowercases(self):
        self.assertEqual(
            registry.parse_twitch_channels("@Demo, #Other,demo"), ["demo", "other"]
        )


class ChannelsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wheelsparser-registry-"))
        self.file = self.tmp / "channels.txt"
        patcher = patch.object(registry, "CHANNELS_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_ignores_comments_and_at_signs(self):
        self.file.write_text(
            "# комментарий\n@one\ntwo # хвост\n\n@one\n", encoding="utf-8"
        )
        self.assertEqual(registry.read_channels_file(), ["one", "two"])

    def test_file_wins_over_env(self):
        self.file.write_text("fromfile\n", encoding="utf-8")
        with patch.dict("os.environ", {"WHEELSPARSER_CHANNELS": "fromenv"}):
            channels, seed = registry.load_channels()
        self.assertEqual(channels, ["fromfile"])
        self.assertFalse(seed)

    def test_env_seeds_when_file_missing(self):
        with patch.dict("os.environ", {"WHEELSPARSER_CHANNELS": "a,b"}):
            channels, seed = registry.load_channels()
        self.assertEqual(channels, ["a", "b"])
        self.assertTrue(seed)

    def test_save_round_trip(self):
        with patch.object(registry, "CHANNELS", ["one", "two"]):
            registry.save_channels_file()
        self.assertEqual(registry.read_channels_file(), ["one", "two"])


class KeywordsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wheelsparser-registry-"))
        self.file = self.tmp / "keywords.txt"
        patcher = patch.object(registry, "KEYWORDS_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_deduplicates_case_insensitively(self):
        self.file.write_text("Колесо\nколесо\nфрибет\n", encoding="utf-8")
        self.assertEqual(registry.read_keywords_file(), ["Колесо", "фрибет"])

    def test_defaults_when_file_missing(self):
        keywords, seed = registry.load_keywords()
        self.assertEqual(keywords, registry.DEFAULT_KEYWORDS)
        self.assertTrue(seed)


class InitTests(unittest.TestCase):
    def test_init_fills_lists_in_place(self):
        channels_ref = registry.CHANNELS  # ссылка, захваченная «другим модулем»
        with patch.object(registry, "load_channels", return_value=(["demo"], False)), \
             patch.object(registry, "load_keywords", return_value=(["слово"], False)), \
             patch.object(
                 registry, "load_twitch_channels", return_value=(["tw"], False)
             ):
            registry.init()
        try:
            self.assertIs(registry.CHANNELS, channels_ref)
            self.assertEqual(channels_ref, ["demo"])
            self.assertEqual(registry.KEYWORDS, ["слово"])
            self.assertEqual(registry.TWITCH_CHANNELS, ["tw"])
        finally:
            registry.CHANNELS[:] = []
            registry.KEYWORDS[:] = []
            registry.TWITCH_CHANNELS[:] = []


if __name__ == "__main__":
    unittest.main()
