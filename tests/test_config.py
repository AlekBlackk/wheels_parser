import os
import unittest
from unittest.mock import patch

from wheelsparser import config

# config читает переменные окружения один раз при импорте, поэтому тесты
# проверяют сами функции env_bool/env_int, а не константы модуля.


class EnvBoolTests(unittest.TestCase):
    def test_empty_value_returns_default(self):
        # Главный регресс: в env.example ключи объявлены как `PRECHECK_WHEELS=`,
        # и скопированный как есть файл молча выключал прекчек — пустая строка
        # трактовалась как False вместо «не задано».
        with patch.dict(os.environ, {"WP_TEST_FLAG": ""}):
            self.assertTrue(config.env_bool("WP_TEST_FLAG", True))
            self.assertFalse(config.env_bool("WP_TEST_FLAG", False))

    def test_whitespace_only_value_returns_default(self):
        with patch.dict(os.environ, {"WP_TEST_FLAG": "   "}):
            self.assertTrue(config.env_bool("WP_TEST_FLAG", True))

    def test_missing_variable_returns_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(config.env_bool("WP_TEST_FLAG", True))
            self.assertFalse(config.env_bool("WP_TEST_FLAG", False))

    def test_recognizes_truthy_values(self):
        for raw in ("1", "true", "yes", "on", "TRUE", "Yes", " on "):
            with self.subTest(raw=raw), patch.dict(os.environ, {"WP_TEST_FLAG": raw}):
                self.assertTrue(config.env_bool("WP_TEST_FLAG", False))

    def test_everything_else_is_false(self):
        for raw in ("0", "false", "no", "off", "да", "2", "enabled"):
            with self.subTest(raw=raw), patch.dict(os.environ, {"WP_TEST_FLAG": raw}):
                self.assertFalse(config.env_bool("WP_TEST_FLAG", True))


class EnvIntTests(unittest.TestCase):
    def test_reads_number(self):
        with patch.dict(os.environ, {"WP_TEST_NUM": "42"}):
            self.assertEqual(config.env_int("WP_TEST_NUM", 10), 42)

    def test_empty_value_returns_default(self):
        with patch.dict(os.environ, {"WP_TEST_NUM": ""}):
            self.assertEqual(config.env_int("WP_TEST_NUM", 10), 10)

    def test_garbage_returns_default(self):
        with patch.dict(os.environ, {"WP_TEST_NUM": "много"}):
            self.assertEqual(config.env_int("WP_TEST_NUM", 10), 10)

    def test_missing_variable_returns_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.env_int("WP_TEST_NUM", 10), 10)

    def test_value_below_minimum_is_raised_to_minimum(self):
        with patch.dict(os.environ, {"WP_TEST_NUM": "1"}):
            self.assertEqual(config.env_int("WP_TEST_NUM", 60, 10), 10)

    def test_negative_value_is_raised_to_minimum(self):
        with patch.dict(os.environ, {"WP_TEST_NUM": "-5"}):
            self.assertEqual(config.env_int("WP_TEST_NUM", 60, 10), 10)


if __name__ == "__main__":
    unittest.main()
