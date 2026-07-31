import unittest
from datetime import timedelta

from wheelsparser import timeutils


class FormatDeadlineTests(unittest.TestCase):
    """Дедлайн колеса в уведомлении и /active: «21:40 (осталось 12 мин)»."""

    def ends_in(self, **delta):
        return (timeutils.now_msk() + timedelta(**delta)).isoformat(timespec="seconds")

    def test_shows_time_and_minutes_left(self):
        self.assertIn(
            "осталось 12 мин",
            timeutils.format_deadline(self.ends_in(minutes=12, seconds=30)),
        )

    def test_shows_hours_for_long_wheels(self):
        self.assertIn(
            "осталось 2 ч 5 мин",
            timeutils.format_deadline(self.ends_in(hours=2, minutes=5, seconds=30)),
        )

    def test_last_minute_is_not_rounded_to_zero(self):
        self.assertIn("осталось 1 мин", timeutils.format_deadline(self.ends_in(seconds=20)))

    def test_past_deadline_says_time_is_up(self):
        # Повторная попытка спустя циклы может застать колесо завершившимся.
        self.assertIn("время вышло", timeutils.format_deadline(self.ends_in(minutes=-1)))

    def test_unknown_deadline_is_empty_string(self):
        # Пустая строка = «срок неизвестен», строку целиком не показываем.
        self.assertEqual(timeutils.format_deadline(""), "")
        self.assertEqual(timeutils.format_deadline(None), "")
        self.assertEqual(timeutils.format_deadline("не дата"), "")

    def test_remaining_can_be_omitted_for_lists(self):
        deadline = timeutils.format_deadline(self.ends_in(minutes=12), remaining=False)
        self.assertNotIn("(", deadline)
        self.assertEqual(len(deadline), 5)  # HH:MM

    def test_naive_timestamp_is_treated_as_msk(self):
        naive = (timeutils.now_msk() + timedelta(minutes=5)).replace(tzinfo=None)
        self.assertIn("осталось", timeutils.format_deadline(naive.isoformat()))


if __name__ == "__main__":
    unittest.main()
